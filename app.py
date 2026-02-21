import streamlit as st
import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
import time
import re
import traceback
import unicodedata

# ==========================================
# 1. ペース解析・展開予想のコアロジック
# ==========================================

def calculate_early_pace_speed(row, current_dist):
    if pd.isna(row.get('early_3f')):
        return np.nan
    
    raw_speed = 600.0 / row['early_3f']
    
    condition_mod = 0.0
    if row['track_type'] == "芝":
        if row['track_condition'] in ["重", "不良"]: condition_mod = +0.15 
        elif row['track_condition'] == "稍": condition_mod = +0.05
    elif row['track_type'] == "ダート":
        if row['track_condition'] in ["重", "不良"]: condition_mod = -0.15 
        elif row['track_condition'] == "稍": condition_mod = -0.05

    course_mod = 0.0
    turf_start_dirt = [("東京", 1600), ("中山", 1200), ("阪神", 1400), ("京都", 1400), ("新潟", 1200), ("中京", 1400)]
    if row['track_type'] == "ダート" and (row['venue'], row['distance']) in turf_start_dirt:
        course_mod += -0.15
        
    uphill_starts = [("中山", 2000, "芝"), ("阪神", 2000, "芝"), ("中京", 2000, "芝")]
    if (row['venue'], row['distance'], row['track_type']) in uphill_starts:
        course_mod += +0.15

    downhill_starts = [("京都", 1400, "芝"), ("京都", 1600, "芝"), ("新潟", 1000, "芝")]
    if (row['venue'], row['distance'], row['track_type']) in downhill_starts:
        course_mod += -0.15

    dist_diff = row['distance'] - current_dist
    clipped_diff = max(-600, min(600, dist_diff))
    distance_mod = (clipped_diff / 100.0) * 0.08

    return raw_speed + condition_mod + course_mod + distance_mod

# 【NEW】戦法キャラクターの判定（控えOK か ハナ絶対 か）
def determine_running_style(past_df: pd.DataFrame) -> str:
    if past_df.empty: return "不明"
    
    # ユーザーのアイデアを実装：好走実績の定義（1着 OR (人気 > 着順 AND 着順 <= 5)）
    is_good_run = (past_df['finish_position'] == 1) | ((past_df['popularity'] > past_df['finish_position']) & (past_df['finish_position'] <= 5))
    good_runs = past_df[is_good_run]
    
    if good_runs.empty: return "不明"
        
    good_positions = good_runs['first_corner_pos'].tolist()
    
    # 全ての好走が「1番手」なら逃げ一辺倒
    if all(pos == 1 for pos in good_positions):
        return "ハナ絶対"
        
    # 2〜5番手での好走実績があれば、譲っても競馬ができる
    if any(2 <= pos <= 5 for pos in good_positions):
        return "控えOK"
        
    return "差し追込"

def extract_jockey_target_position(past_races_df: pd.DataFrame, current_venue: str) -> float:
    if past_races_df.empty: return 7.0 
    
    is_success = (past_races_df['finish_position'] == 1) | (past_races_df['popularity'] > past_races_df['finish_position'])
    is_same_venue = past_races_df['venue'] == current_venue
    
    venue_success_races = past_races_df[is_success & is_same_venue]
    if not venue_success_races.empty:
        return float(venue_success_races.iloc[0]['first_corner_pos'])
    
    success_races = past_races_df[is_success]
    if not success_races.empty:
        return float(success_races.iloc[0]['first_corner_pos'])
        
    return float(past_races_df['first_corner_pos'].mean())

def calculate_pace_score(horse, current_dist, current_venue, current_track, total_horses):
    past_df = pd.DataFrame(horse['past_races'])
    if past_df.empty: 
        horse['condition_mod'] = 0.0
        horse['special_flag'] = ""
        horse['max_early_speed'] = 16.0
        horse['running_style'] = "不明"
        return 7.0 
    
    horse['running_style'] = determine_running_style(past_df)
    
    past_df['early_speed'] = past_df.apply(lambda row: calculate_early_pace_speed(row, current_dist), axis=1)
    max_speed = past_df['early_speed'].max()
    horse['max_early_speed'] = max_speed if not pd.isna(max_speed) else 16.0
    
    speed_advantage = 0.0
    if not pd.isna(max_speed):
        speed_advantage = (16.8 - max_speed) * 3.0 

    jockey_target = extract_jockey_target_position(past_df, current_venue)
    base_position = (jockey_target * 0.6) + speed_advantage
    
    last_race = past_df.iloc[0]
    weight_modifier = (horse['current_weight'] - last_race['weight']) * 0.25
    
    base_mod = (horse['horse_number'] - 1) * 0.05 
    outside_adv_courses = [("中山", 1200, "ダート"), ("東京", 1600, "ダート"), ("阪神", 1400, "ダート"), ("京都", 1400, "ダート")]
    if (current_venue, current_dist, current_track) in outside_adv_courses:
        base_mod = (total_horses - horse['horse_number']) * 0.02 - 0.15

    late_start_penalty = 0.0
    horse['special_flag'] = ""
    
    if last_race.get('is_late_start', False):
        late_start_penalty += 1.0 
        if last_race['first_corner_pos'] <= 5:
            is_past_outside = last_race['past_frame'] >= 5
            is_current_inside = horse['horse_number'] <= (total_horses / 2) 
            
            if is_past_outside and is_current_inside:
                late_start_penalty += 2.5
                horse['special_flag'] = "⚠️前走外枠リカバー→今回内枠で包まれ出遅れリスク"
            elif is_past_outside and not is_current_inside:
                late_start_penalty -= 0.5
                horse['special_flag'] = "🐎出遅れ癖ありも外枠からリカバー警戒"
            elif not is_past_outside:
                horse['special_flag'] = "🔥出遅れを内からリカバリーする鬼脚"

    final_score = base_position + weight_modifier + base_mod + late_start_penalty
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
    
    parts = []
    if leaders: parts.append(f"({''.join(leaders)})")
    if chasers: parts.append("".join(chasers))
    if mid: parts.append("".join(mid))
    if backs: parts.append("".join(backs))
    return " ".join(parts)

# 【NEW】キャラクター衝突ベースのペース判定
def generate_pace_and_spread_comment(sorted_horses, current_track):
    if len(sorted_horses) < 3: return "データ不足"
    
    top_score = sorted_horses[0]['score']
    leaders = [h for h in sorted_horses if h['score'] <= top_score + 1.2][:3]
    leader_nums = "、".join([chr(9311 + h['horse_number']) for h in leaders])
    
    mid_idx = min(len(sorted_horses)-1, int(len(sorted_horses) * 0.6))
    spread_gap = sorted_horses[mid_idx]['score'] - top_score
    
    if spread_gap >= 5.0:
        spread_text = "隊列は【縦長】"
        spread_reason = "テンが速い馬と遅い馬のスピード差が激しく、ばらけた展開になりそうです。"
    elif spread_gap <= 2.5:
        spread_text = "馬群は【一団】"
        spread_reason = "各馬の前半スピードが拮抗しており、密集した塊のまま進む展開が濃厚です。コース取りの差が出やすくなります。"
    else:
        spread_text = "【標準的な隊列】"
        spread_reason = "極端にばらけることもなく、標準的なペース配分になりそうです。"
        
    top3_speeds = [h.get('max_early_speed', 16.1) for h in leaders]
    avg_top_speed = sum(top3_speeds) / len(top3_speeds) if top3_speeds else 16.1
    high_pace_threshold = 16.7 if current_track == "芝" else 16.5
    slow_pace_threshold = 16.3 if current_track == "芝" else 16.1

    # ユーザーアイデア：気性・戦法による判定
    must_lead_count = sum(1 for h in leaders if h.get('running_style') == "ハナ絶対")
    can_wait_count = sum(1 for h in leaders if h.get('running_style') == "控えOK")

    if must_lead_count >= 2:
        base_cmt = f"🔥 ハイペース必至\n「何がなんでも逃げたい」馬が複数おり、{leader_nums}の激しい先行争いでテンは速くなりそうです。"
    elif must_lead_count == 1 and avg_top_speed >= high_pace_threshold:
        base_cmt = f"🏃 ややハイペース想定\n逃げ主張馬がペースを作り、{leader_nums}が引っ張る淀みない流れになりそうです。"
    elif must_lead_count == 0 and can_wait_count >= 2:
        base_cmt = f"🚶 ややスローペース想定\n{leader_nums}が前に行きますが、「控えても結果を出せる」馬たちなので互いに牽制し合い、ペースは落ち着きそうです。"
    elif avg_top_speed < slow_pace_threshold:
        base_cmt = f"🐢 スローペース想定\n全体的にテンのダッシュ力が控えめで、{leader_nums}が楽に主導権を握る展開。後続は折り合い重視になりそうです。"
    else:
        base_cmt = f"🐎 平均ペース想定\n{leader_nums}が並んで先行しますが、無理のない標準的なペース配分になりそうです。"

    special_alerts = [chr(9311 + h['horse_number']) + " " + h['special_flag'] for h in sorted_horses if h.get('special_flag')]
    
    final_cmt = f"**{spread_text}**\n{spread_reason}\n\n**{base_cmt}**"
    if special_alerts:
        final_cmt += "\n\n💡 **特注ポイント**\n・" + "\n・".join(special_alerts)
        
    return final_cmt

# ==========================================
# 2. 競馬ブック スクレイピングロジック
# ==========================================
def fetch_real_data(race_id: str):
    url = f"https://s.keibabook.co.jp/cyuou/nouryoku_html_detail/{race_id}.html"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8' 
        time.sleep(1) 
        soup = BeautifulSoup(response.text, 'html.parser')
        
        basyo_elem = soup.select_one('td.basyo')
        current_venue = basyo_elem.text.strip() if basyo_elem else "不明"
        if current_venue == "不明": return None, 1600, "", "芝", "出馬表データが見つかりません。"
        
        kyori_elem = soup.select_one('span.kyori')
        course_elem = soup.select_one('span.course')
        
        current_dist = int(re.search(r'\d+', kyori_elem.text).group()) if kyori_elem else 1600
        current_track = "ダート" if course_elem and "ダ" in course_elem.text else "芝"

        horses_data = []
        trs = soup.select('table.noryoku tr[class^="js-umaban"]')
        if not trs:
            return None, current_dist, current_venue, current_track, "出走馬データが見つかりません。"

        for tr in trs:
            umaban_elem = tr.select_one('td.umaban span')
            if not umaban_elem: continue
            horse_num = int(umaban_elem.text.strip())
            
            bamei_elem = tr.select_one('td.bamei span.kbamei a')
            horse_name = bamei_elem.text.strip() if bamei_elem else "不明"
            
            past_races = []
            current_weight = 480.0 
            
            for td in tr.select('td.zensou'):
                if not td.select_one('.kyori'): continue
                
                k_text = td.select_one('.kyori').text
                dist_m = re.search(r'\d+', k_text)
                dist = int(dist_m.group()) if dist_m else current_dist
                track = "ダート" if "ダ" in k_text else "芝"
                
                baba_img = td.select_one('.baba img')
                baba_cond = "良"
                if baba_img:
                    src = baba_img.get('src', '')
                    if 'ryo' in src: baba_cond = '良'
                    elif 'yaya' in src: baba_cond = '稍'
                    elif 'omo' in src: baba_cond = '重'
                    elif 'huryo' in src: baba_cond = '不良'
                
                early_3f_span = td.select_one('.uzenh3')
                early_3f = np.nan
                if early_3f_span:
                    e3f_text = early_3f_span.text.strip()
                    e3f_match = re.search(r'[\d\.]+', e3f_text)
                    if e3f_match:
                        try:
                            val = float(e3f_match.group())
                            if 25.0 <= val <= 60.0:
                                early_3f = val
                        except:
                            pass
                
                tuka_imgs = td.select('.tuka img')
                first_corner = 7
                is_late_start = False
                if tuka_imgs:
                    src = tuka_imgs[0].get('src', '')
                    m = re.search(r'(\d+)\.gif', src)
                    if m: first_corner = int(m.group(1))
                    if 'maru' in src: is_late_start = True 
                        
                umaban_span = td.select_one('.umaban')
                past_frame = 4
                if umaban_span:
                    frame_m = re.search(r'(\d+)枠', umaban_span.text)
                    if frame_m: past_frame = int(frame_m.group(1))

                cyaku_span = td.select_one('span[class^="cyaku"]')
                finish_pos = int(re.search(r'\d+', cyaku_span.text).group()) if cyaku_span and re.search(r'\d+', cyaku_span.text) else 5
                
                ninki_span = td.select_one('.ninki')
                popularity = int(re.search(r'\d+', ninki_span.text).group()) if ninki_span and re.search(r'\d+', ninki_span.text) else 5
                
                negahi_spans = td.select('.negahi')
                p_venue = current_venue
                if negahi_spans:
                    v_text = negahi_spans[0].text
                    venue_map = {"東":"東京", "中":"中山", "京":"京都", "阪":"阪神", "名":"中京", "新":"新潟", "福":"福島", "小":"小倉", "札":"札幌", "函":"函館"}
                    for v_key, v_val in venue_map.items():
                        if v_key in v_text:
                            p_venue = v_val
                            break
                
                batai_span = td.select_one('.batai')
                weight = float(batai_span.text.strip()) if batai_span else 480.0
                
                if len(past_races) == 0:
                    current_weight = weight
                
                past_races.append({
                    'venue': p_venue, 'track_type': track, 'distance': dist,
                    'track_condition': baba_cond, 'finish_position': finish_pos,
                    'popularity': popularity, 'early_3f': early_3f,
                    'first_corner_pos': first_corner, 'is_late_start': is_late_start,
                    'past_frame': past_frame, 'weight': weight
                })

            horses_data.append({
                'horse_number': horse_num, 'horse_name': horse_name,
                'current_weight': current_weight, 'past_races': past_races,
                'score': 0.0, 'special_flag': ""
            })

        if not horses_data: return None, 1600, "", "芝", "馬データが取得できませんでした。"
        
        return horses_data, current_dist, current_venue, current_track, None
        
    except Exception as e:
        return None, 1600, "", "芝", f"エラー: {e}\n{traceback.format_exc()}"

# ==========================================
# 3. スマホ対応UI
# ==========================================
st.set_page_config(page_title="AI競馬展開予想", page_icon="🏇", layout="centered")

st.title("🏇 AI競馬展開予想 (気性×絶対スピード版)")
st.markdown("前走までの「好走パターン」から各馬の戦法をプロファイリングし、リアルな隊列とペースを推測します。")

with st.container(border=True):
    st.subheader("⚙️ レース設定")
    base_url_input = st.text_input("🔗 競馬ブックのレースURL", value="https://s.keibabook.co.jp/cyuou/nouryoku_html_detail/202601040703.html")
    
    st.markdown("**🎯 予想したいレースを選択（複数可）**")
    
    try:
        selected_races = st.pills("レース番号", options=list(range(1, 13)), default=[11], format_func=lambda x: f"{x}R", selection_mode="multi")
    except TypeError:
        selected_races = st.multiselect("レース番号", options=list(range(1, 13)), default=[11], format_func=lambda x: f"{x}R")

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
    match = re.search(r'\d{12}', base_url_input)
    if not match:
        st.error("有効な競馬ブックのレースIDが見つかりません。")
        st.stop()
        
    base_id = match.group()[:10]
    
    for race_num in sorted(races_to_run):
        target_race_id = f"{base_id}{race_num:02d}"
        
        st.markdown(f"### 🏁 {race_num}R")
        
        with st.spinner(f"{race_num}R のデータを解析中..."):
            horses, current_dist, current_venue, current_track, error_msg = fetch_real_data(target_race_id)
            
            if error_msg:
                st.warning(f"{error_msg}")
                continue
                
            total_horses = len(horses)
            
            for horse in horses:
                horse['score'] = calculate_pace_score(horse, current_dist, current_venue, current_track, total_horses)
                
            sorted_horses = sorted(horses, key=lambda x: x['score'])
            formation_text = format_formation(sorted_horses)
            
            pace_comment = generate_pace_and_spread_comment(sorted_horses, current_track)

            st.info(f"📏 条件: **{current_venue} {current_track}{current_dist}m** ({total_horses}頭立て)")
            
            st.markdown(f"<h4 style='text-align: center; letter-spacing: 2px;'>◀(進行方向)</h4>", unsafe_allow_html=True)
            st.markdown(f"<h3 style='text-align: center; color: #FF4B4B;'>{formation_text}</h3>", unsafe_allow_html=True)
            
            st.markdown("---")
            st.write(pace_comment)
            
            with st.expander(f"📊 {race_num}R の詳細データを見る"):
                df_result = pd.DataFrame([{
                    "馬番": h['horse_number'],
                    "馬名": h['horse_name'],
                    "スコア": round(h['score'], 2),
                    "戦法": h.get('running_style', ''),
                    "特記事項": h.get('special_flag', '')
                } for h in sorted_horses])
                st.dataframe(df_result, use_container_width=True, hide_index=True)
            
            st.markdown("<br>", unsafe_allow_html=True)
