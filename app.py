import streamlit as st
import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
import time
import re
import traceback

# ==========================================
# 1. 展開予想のコアロジック
# ==========================================
def extract_jockey_target_position(past_races_df: pd.DataFrame) -> float:
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

def calculate_pace_score(horse, current_dist):
    past_df = pd.DataFrame(horse['past_races'])
    if past_df.empty: return 7.0 
    
    # 【NEW】出遅れノイズカット：平均値(mean)ではなく中央値(median)を使用し、突発的な不利を無視する
    recent_3_median = past_df.head(3)['first_corner_pos'].median()
    jockey_target = extract_jockey_target_position(past_df)
    base_position = (recent_3_median * 0.6) + (jockey_target * 0.4)
    
    last_race = past_df.iloc[0]
    
    # 【NEW】昇級戦ショック：前走1着馬は相手強化でペースが速くなり、相対的に前に行きにくくなるためペナルティを加算
    promotion_penalty = 1.0 if last_race['finish_position'] == 1 else 0.0
    
    dist_diff = last_race['distance'] - current_dist
    clipped_diff = max(-400, min(400, dist_diff))
    dist_modifier = (clipped_diff / 100.0) * 0.2 
    weight_modifier = (horse['current_weight'] - last_race['weight']) * 0.25
    local_modifier = -1.0 if last_race['is_local'] else 0.0
    frame_modifier = (horse['horse_number'] - 1) * 0.05
    
    # 全ファクターの合算
    final_score = base_position + dist_modifier + weight_modifier + local_modifier + frame_modifier + promotion_penalty
    return max(1.0, min(18.0, final_score))

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
    if len(leaders) >= 3: return f"🔥 ハイペース\n{leader_nums}が激しくハナを主張し合い、テンは早くなりそう。縦長。"
    elif len(leaders) == 2 and gap_to_second < 0.5: return f"🏃 平均ペース\n{leader_nums}が並んで先行争い。隊列はすんなり決まりそう。"
    elif gap_to_second >= 1.5: return f"🐢 スローペース\n{leader_nums}が楽に単騎逃げ。後続は折り合い重視の展開。"
    else: return f"🚶 平均〜スローペース\n{leader_nums}が主導権を握るが、競りかける馬はおらず落ち着きそう。"

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
        if not soup.select_one('#denma_latest'): return None, 1600, "出馬表データが見つかりませんでした。"
        
        current_venue = ""
        venue_elem = soup.select_one('.hr-menuWhite__item--current .hr-menuWhite__text')
        if venue_elem: current_venue = venue_elem.text.strip()
            
        current_dist = 1600 
        status_div = soup.select_one('.hr-predictRaceInfo__status')
        if status_div:
            dist_match = re.search(r'(\d{4})m', status_div.text)
            if dist_match: current_dist = int(dist_match.group(1))

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
                
                is_same_venue = False
                date_spans = td.select('.hr-denma__date span')
                if len(date_spans) >= 2 and current_venue and date_spans[1].text.strip() in current_venue: is_same_venue = True
                elif current_venue and current_venue in txt: is_same_venue = True

                past_j_elem = td.select_one('.hr-denma__jockey')
                past_weight = float(re.search(r'\((\d{2}(?:\.\d)?)\)', past_j_elem.text).group(1)) if past_j_elem and re.search(r'\((\d{2}(?:\.\d)?)\)', past_j_elem.text) else current_weight

                past_races.append({
                    'finish_position': finish_pos, 'popularity': popularity,
                    'first_corner_pos': first_corner, 'distance': distance,
                    'weight': past_weight, 'is_local': is_local, 'is_same_venue': is_same_venue
                })
            horses_data.append({
                'horse_number': horse_num, 'horse_name': horse_name,
                'current_weight': current_weight, 'past_races': past_races
            })
        if not horses_data: return None, current_dist, "馬柱データが見つかりません。"
        return horses_data, current_dist, None
    except Exception as e:
        return None, 1600, f"エラー: {e}\n{traceback.format_exc()}"

# ==========================================
# 3. スマホ対応UI (UI修正)
# ==========================================
st.set_page_config(page_title="スマホで競馬展開予想", page_icon="🏇", layout="centered")

st.title("🏇 AI競馬展開予想")
st.markdown("スマホでURLをコピペして、サクッと隊列とペースを予測します。")

# 入力エリアをカード風にまとめる
with st.container(border=True):
    st.subheader("⚙️ レース設定")
    base_url_input = st.text_input("🔗 Yahoo!競馬のURL (どれか1レースでOK)", value="https://sports.yahoo.co.jp/keiba/race/denma/2605010711?detail=1", placeholder="ここにURLをペースト")
    
    st.markdown("**🎯 予想したいレースを選択（複数可）**")
    # 確実な複数選択ができるmultiselectを採用
    selected_races = st.multiselect("レース番号", options=list(range(1, 13)), default=[11], format_func=lambda x: f"{x}R")

    # ボタンを横並びに配置（全レースボタン追加）
    col1, col2 = st.columns(2)
    with col1:
        execute_btn = st.button("🚀 選択レースを予想", type="primary", use_container_width=True)
    with col2:
        execute_all_btn = st.button("🌟 全12Rを一括予想", type="secondary", use_container_width=True)

# どちらのボタンが押されたかで処理を分岐
races_to_run = []
if execute_all_btn:
    races_to_run = list(range(1, 13))
elif execute_btn:
    if not selected_races:
        st.warning("レース番号を選択してください。")
        st.stop()
    races_to_run = selected_races

# 実行処理ループ
if races_to_run:
    match = re.search(r'\d{10}', base_url_input)
    if not match:
        st.error("有効なYahoo!競馬のレースID(10桁)が見つかりません。")
        st.stop()
        
    base_id = match.group()[:8] 
    
    for race_num in sorted(races_to_run):
        target_race_id = f"{base_id}{race_num:02d}"
        
        st.markdown(f"### 🏁 {race_num}R")
        
        with st.spinner(f"{race_num}Rを解析中..."):
            horses, current_dist, error_msg = fetch_real_data(target_race_id)
            
            if error_msg:
                st.warning(f"出馬表データがまだ確定していないか、取得できませんでした。")
                continue
                
            for horse in horses:
                horse['score'] = calculate_pace_score(horse, current_dist)
                
            sorted_horses = sorted(horses, key=lambda x: x['score'])
            formation_text = format_formation(sorted_horses)
            comment = generate_short_comment(sorted_horses)

            st.info(f"📏 距離: **{current_dist}m**")
            
            st.markdown(f"<h4 style='text-align: center; letter-spacing: 2px;'>◀(進行方向)</h4>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center; color: #FF4B4B;'>{formation_text}</h3>", unsafe_allow_html=True)
            
            st.markdown("---")
            st.write(comment)
            
            with st.expander(f"📊 {race_num}R の詳細スコアを見る"):
                df_result = pd.DataFrame([{
                    "馬番": h['horse_number'],
                    "馬名": h['horse_name'],
                    "スコア": round(h['score'], 2),
                    "斤量": h['current_weight']
                } for h in sorted_horses])
                st.dataframe(df_result, use_container_width=True, hide_index=True)
                
        st.markdown("<br><br>", unsafe_allow_html=True)
