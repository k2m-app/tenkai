import streamlit as st
import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
import time
import re
import traceback

# ==========================================
# 1. 展開予想のコアロジック (超・専門家アップデート)
# ==========================================

def extract_jockey_target_position(past_races_df: pd.DataFrame) -> float:
    """成功体験バイアス（騎手心理＋同コース適性）"""
    if past_races_df.empty: return 7.0 
    is_success = (past_races_df['finish_position'] == 1) | (past_races_df['popularity'] > past_races_df['finish_position'])
    success_races = past_races_df[is_success]
    if not success_races.empty:
        upset_score = success_races['popularity'] - success_races['finish_position']
        win_bonus = np.where(success_races['finish_position'] == 1, 10, 0)
        same_venue_bonus = np.where(success_races.get('is_same_venue', False), 8, 0)
        success_score = upset_score + win_bonus + same_venue_bonus
        best_memory_idx = success_score.idxmax()
        return float(past_races_df.loc[best_memory_idx, 'first_corner_pos'])
    else:
        return float(past_races_df['first_corner_pos'].mean())

def get_frame_specific_base_position(past_df, current_horse_num, total_horses):
    """今回の枠（内・外）と同じ枠だった過去走のテンの速さを優先する"""
    if past_df.empty: return 7.0
    
    is_current_inside = current_horse_num <= (total_horses / 2)
    
    def check_inside(row):
        return row['past_horse_num'] <= (row['past_total_horses'] / 2)
        
    past_df['is_inside'] = past_df.apply(check_inside, axis=1)
    same_frame_df = past_df[past_df['is_inside'] == is_current_inside]
    
    if len(same_frame_df) >= 2:
        return same_frame_df['first_corner_pos'].median()
    else:
        return past_df['first_corner_pos'].median()

def get_frame_modifier(venue, dist, track_type, horse_num, total_horses):
    """コース形態による枠順バイアスの最適化"""
    base_mod = (horse_num - 1) * 0.05 
    
    outside_adv_courses = [
        ("中山", 1200, "ダート"), ("東京", 1600, "ダート"),
        ("阪神", 1400, "ダート"), ("京都", 1400, "ダート"),
        ("新潟", 1000, "芝")
    ]
    
    if (venue, dist, track_type) in outside_adv_courses:
        base_mod = (total_horses - horse_num) * 0.05 - 0.4
        
    return base_mod

def check_escape_only_horse(past_df: pd.DataFrame) -> bool:
    """【新機能】JRAで逃げた時だけ馬券に絡む（番手だとダメな）不器用な馬か判定"""
    if past_df.empty: return False
    
    # 地方交流戦は小回りで逃げ残りやすいため除外し、JRAのレースのみで判断
    jra_df = past_df[~past_df['is_local']]
    if jra_df.empty: return False
    
    escape_races = jra_df[jra_df['first_corner_pos'] == 1]
    non_escape_races = jra_df[jra_df['first_corner_pos'] > 1]
    
    # 逃げた経験がないなら対象外
    if escape_races.empty: return False 
        
    # 逃げた時に3着以内に入ったことがあるか
    escape_success = (escape_races['finish_position'] <= 3).any()
    if not escape_success: return False
        
    # 逃げられなかった時（番手以下）に馬券に絡んだ（3着以内）ことがあるか？
    # あれば「控えても大丈夫な馬」なので逃げ専用機からは除外
    if not non_escape_races.empty:
        non_escape_success = (non_escape_races['finish_position'] <= 3).any()
        if non_escape_success: return False 
            
    # 「逃げて好走した」かつ「控えて好走したことがない」馬
    return True

def calculate_pace_score(horse, current_dist, current_venue, current_track, total_horses):
    """各馬の1次ポジションスコアを算出"""
    past_df = pd.DataFrame(horse['past_races'])
    if past_df.empty: 
        horse['condition_mod'] = 0.0
        horse['special_flag'] = ""
        return 7.0 
    
    frame_specific_median = get_frame_specific_base_position(past_df, horse['horse_number'], total_horses)
    jockey_target = extract_jockey_target_position(past_df)
    base_position = (frame_specific_median * 0.6) + (jockey_target * 0.4)
    
    last_race = past_df.iloc[0]
    promotion_penalty = 1.0 if last_race['finish_position'] == 1 else 0.0
    
    dist_diff = last_race['distance'] - current_dist
    clipped_diff = max(-400, min(400, dist_diff))
    dist_modifier = (clipped_diff / 100.0) * 0.2 
    
    weight_modifier = (horse['current_weight'] - last_race['weight']) * 0.25
    local_modifier = -1.0 if last_race['is_local'] else 0.0
    frame_modifier = get_frame_modifier(current_venue, current_dist, current_track, horse['horse_number'], total_horses)
    
    # 【改修】近走調子バイアス（穴馬を残すため、6着以下でもペナルティはなし）
    recent_3_races = past_df.head(3)
    if (recent_3_races['finish_position'] <= 5).any():
        condition_modifier = -0.5 # 好調：1つくらい位置取りが上がる
    else:
        condition_modifier = 0.0  # 不調でもペナルティなし
    horse['condition_mod'] = condition_modifier 
    
    # 【NEW】逃げ専用機バイアス（ハナ絶対宣言）
    is_escape_only = check_escape_only_horse(past_df)
    escape_modifier = -2.5 if is_escape_only else 0.0
    horse['special_flag'] = "🔥逃げ専用(ハナ絶対)" if is_escape_only else ""

    final_score = base_position + dist_modifier + weight_modifier + local_modifier + frame_modifier + promotion_penalty + condition_modifier + escape_modifier
    return max(1.0, min(18.0, final_score))

def apply_position_synergy(horses):
    """内枠の逃げ馬による番手恩恵（スリップストリーム効果）"""
    horses_sorted = sorted(horses, key=lambda x: x['horse_number'])
    
    for i in range(len(horses_sorted)):
        current_score = horses_sorted[i]['score']
        if 2.5 <= current_score <= 6.0:
            inner_horses = horses_sorted[max(0, i-2):i]
            for inner_h in inner_horses:
                if inner_h['score'] <= 2.0:
                    horses_sorted[i]['score'] -= 0.8
                    horses_sorted[i]['synergy'] = "内枠逃げ馬の恩恵"
                    break 
                    
    return horses_sorted

def format_formation(sorted_horses):
    if not sorted_horses: return ""
    leaders, chasers, mid, backs = [], [], [], []
    top_score = sorted_horses[0]['score']
    for h in sorted_horses:
        num_str = chr(9311 + h['horse_number']) 
        score = h['score']
        if score <= top_score + 1.2 and len(leaders) < 3: leaders.append(num_str)
        elif score <= top_score + 4.5: chasers.append(num_str)
        elif score <= top_score + 9.5: mid.append(num_str)
        else: backs.append(num_str)
    if not leaders and sorted_horses:
        leaders.append(chr(9311 + sorted_horses[0]['horse_number']))
        if chasers and chasers[0] == leaders[0]: chasers.pop(0)
    parts = []
    if leaders: parts.append(f"({''.join(leaders)})")
    if chasers: parts.append("".join(chasers))
    if mid: parts.append("".join(mid))
    if backs: parts.append("".join(backs))
    return " ".join(parts)

def generate_short_comment(sorted_horses):
    if len(sorted_horses) < 2: return "データ不足"
    top_score = sorted_horses[0]['score']
    leaders = [h for h in sorted_horses if h['score'] <= top_score + 1.2][:3]
    leader_nums = "と".join([chr(9311 + h['horse_number']) for h in leaders])
    gap_to_second = sorted_horses[1]['score'] - top_score
    
    synergy_horses = [chr(9311 + h['horse_number']) for h in sorted_horses if h.get('synergy')]
    synergy_text = f"内枠の逃げ馬を利用して{synergy_horses[0]}が絶好の番手を取れそう。" if synergy_horses else ""
    
    escape_only_horses = [chr(9311 + h['horse_number']) for h in sorted_horses if h.get('special_flag')]
    escape_text = f"何としてもハナを切りたい{escape_only_horses[0]}がペースを引き上げる。" if escape_only_horses else ""

    if len(leaders) >= 3: base_cmt = f"🔥 ハイペース\n{leader_nums}が激しくハナを主張し合い、テンは早くなりそう。縦長。"
    elif len(leaders) == 2 and gap_to_second < 0.5: base_cmt = f"🏃 平均ペース\n{leader_nums}が並んで先行争い。隊列はすんなり決まりそう。"
    elif gap_to_second >= 1.5: base_cmt = f"🐢 スローペース\n{leader_nums}が楽に単騎逃げ。後続は折り合い重視の展開。"
    else: base_cmt = f"🚶 平均〜スローペース\n{leader_nums}が主導権を握るが、競りかける馬はおらず落ち着きそう。"
    
    final_cmt = base_cmt
    if escape_text: final_cmt += "\n⚠️ " + escape_text
    if synergy_text: final_cmt += "\n💡 " + synergy_text
    return final_cmt

# ==========================================
# 2. スクレイピングロジック
# ==========================================
def fetch_real_data(race_id: str):
    url = f"https://sports.yahoo.co.jp/keiba/race/denma/{race_id}?detail=1"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8' 
        time.sleep(1) 
        soup = BeautifulSoup(response.text, 'html.parser')
        if not soup.select_one('#denma_latest'): return None, 1600, "", "芝", "出馬表が見つかりません。"
        
        current_venue = ""
        venue_elem = soup.select_one('.hr-menuWhite__item--current .hr-menuWhite__text')
        if venue_elem: current_venue = venue_elem.text.strip()
            
        current_dist = 1600 
        current_track = "芝"
        status_div = soup.select_one('.hr-predictRaceInfo__status')
        if status_div:
            dist_match = re.search(r'(\d{4})m', status_div.text)
            if dist_match: current_dist = int(dist_match.group(1))
            
            track_match = re.search(r'(芝|ダート|障害)', status_div.text)
            if track_match: current_track = track_match.group(1)

        horses_data = []
        for tr_latest, tr_past in zip(soup.select('#denma_latest tbody tr'), soup.select('#denma_past tbody tr')):
            num_elem = tr_latest.select_one('.hr-denma__number')
            if not num_elem: continue
            horse_num = int(num_elem.text.strip())
            name_elem = tr_latest.select_one('.hr-denma__horse a')
            horse_name = name_elem.text.strip() if name_elem else "不明"
            info_td = tr_past.select_one('.hr-tableScroll__data--name')
            current_weight = 55.0
            if info_td and info_td.find_all('p'):
                try: current_weight = float(info_td.find_all('p')[-1].text.strip())
                except: pass

            past_races = []
            for td in tr_past.select('.hr-tableScroll__data--race'):
                arr_elem = td.select_one('.hr-denma__arrival')
                if not arr_elem: continue 
                try: finish_pos = int(re.search(r'\d+', arr_elem.text).group())
                except: continue 
                txt = td.text
                pop_match = re.search(r'\((\d+)人気\)', txt)
                popularity = int(pop_match.group(1)) if pop_match else 7
                pass_elem = td.select_one('.hr-denma__passing')
                first_corner = int(re.search(r'^(\d+)', pass_elem.text.strip()).group(1)) if pass_elem and re.search(r'^(\d+)', pass_elem.text.strip()) else 7
                dist_match_past = re.search(r'(\d{4})m', txt)
                distance = int(dist_match_past.group(1)) if dist_match_past else current_dist
                is_local = any(loc in txt for loc in ["川崎", "大井", "船橋", "浦和", "門別", "盛岡", "水沢", "園田", "姫路", "高知", "佐賀", "名古屋", "笠松", "金沢", "帯広"])
                
                horse_num_match = re.search(r'(\d+)頭\s+(\d+)番', txt)
                past_total_horses = int(horse_num_match.group(1)) if horse_num_match else 16
                past_horse_num = int(horse_num_match.group(2)) if horse_num_match else 8

                is_same_venue = False
                date_spans = td.select('.hr-denma__date span')
                if len(date_spans) >= 2 and current_venue and date_spans[1].text.strip() in current_venue: is_same_venue = True
                elif current_venue and current_venue in txt: is_same_venue = True

                past_j_elem = td.select_one('.hr-denma__jockey')
                past_weight = float(re.search(r'\((\d{2}(?:\.\d)?)\)', past_j_elem.text).group(1)) if past_j_elem and re.search(r'\((\d{2}(?:\.\d)?)\)', past_j_elem.text) else current_weight

                past_races.append({
                    'finish_position': finish_pos, 'popularity': popularity,
                    'first_corner_pos': first_corner, 'distance': distance,
                    'weight': past_weight, 'is_local': is_local, 'is_same_venue': is_same_venue,
                    'past_total_horses': past_total_horses, 'past_horse_num': past_horse_num
                })
            horses_data.append({
                'horse_number': horse_num, 'horse_name': horse_name,
                'current_weight': current_weight, 'past_races': past_races,
                'synergy': "", 'condition_mod': 0.0, 'special_flag': ""
            })
        if not horses_data: return None, 1600, "", "芝", "データがありません。"
        return horses_data, current_dist, current_venue, current_track, None
    except Exception as e:
        return None, 1600, "", "芝", f"エラー: {e}\n{traceback.format_exc()}"

# ==========================================
# 3. スマホ対応UI
# ==========================================
st.set_page_config(page_title="スマホで競馬展開予想", page_icon="🏇", layout="centered")

st.title("🏇 AI競馬展開予想")
st.markdown("枠順バイアス・調子・隣接馬とのシナジーまで考慮するプロ仕様の隊列予測です。")

with st.container(border=True):
    st.subheader("⚙️ レース設定")
    base_url_input = st.text_input("🔗 Yahoo!競馬のURL (どれか1レースでOK)", value="https://sports.yahoo.co.jp/keiba/race/denma/2605010711?detail=1")
    
    st.markdown("**🎯 予想したいレースを選択（複数可）**")
    
    try:
        selected_races = st.pills("レース番号", options=list(range(1, 13)), default=[11], format_func=lambda x: f"{x}R", selection_mode="multi")
    except TypeError:
        selected_races = st.pills("レース番号", options=list(range(1, 13)), default=11, format_func=lambda x: f"{x}R")

    if not isinstance(selected_races, list):
        if selected_races is None:
            selected_races = []
        else:
            selected_races = [selected_races]

    col1, col2 = st.columns(2)
    with col1:
        execute_btn = st.button("🚀 選択レースを予想", type="primary", use_container_width=True)
    with col2:
        execute_all_btn = st.button("🌟 全12Rを一括予想", type="secondary", use_container_width=True)

races_to_run = []
if execute_all_btn:
    races_to_run = list(range(1, 13))
elif execute_btn:
    if not selected_races:
        st.warning("レース番号を選択してください。")
        st.stop()
    races_to_run = selected_races

if races_to_run:
    match = re.search(r'\d{10}', base_url_input)
    if not match:
        st.error("有効なYahoo!競馬のレースIDが見つかりません。")
        st.stop()
        
    base_id = match.group()[:8] 
    
    for race_num in sorted(races_to_run):
        target_race_id = f"{base_id}{race_num:02d}"
        
        st.markdown(f"### 🏁 {race_num}R")
        
        with st.spinner(f"{race_num}Rを解析中..."):
            horses, current_dist, current_venue, current_track, error_msg = fetch_real_data(target_race_id)
            
            if error_msg:
                st.warning("出馬表データがまだ確定していないか、取得できませんでした。")
                continue
                
            total_horses = len(horses)
            
            # 1次スコア計算
            for horse in horses:
                horse['score'] = calculate_pace_score(horse, current_dist, current_venue, current_track, total_horses)
            
            # 2次スコア計算 (内枠逃げ馬による番手恩恵シナジー)
            horses = apply_position_synergy(horses)
                
            sorted_horses = sorted(horses, key=lambda x: x['score'])
            formation_text = format_formation(sorted_horses)
            comment = generate_short_comment(sorted_horses)

            st.info(f"📏 条件: **{current_venue} {current_track}{current_dist}m** ({total_horses}頭立て)")
            
            st.markdown(f"<h4 style='text-align: center; letter-spacing: 2px;'>◀(進行方向)</h4>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center; color: #FF4B4B;'>{formation_text}</h3>", unsafe_allow_html=True)
            
            st.markdown("---")
            st.write(comment)
            
            with st.expander(f"📊 {race_num}R の詳細スコアを見る"):
                df_result = pd.DataFrame([{
                    "馬番": h['horse_number'],
                    "馬名": h['horse_name'],
                    "スコア": round(h['score'], 2),
                    "斤量差": f"{round(h['current_weight'] - h['past_races'][0]['weight'], 1):+}" if h['past_races'] else "-",
                    "調子補正": f"{h.get('condition_mod', 0.0):+}",
                    "特記事項": f"{h.get('special_flag', '')} {h.get('synergy', '')}".strip()
                } for h in sorted_horses])
                st.dataframe(df_result, use_container_width=True, hide_index=True)
                
        st.markdown("<br><br>", unsafe_allow_html=True)
