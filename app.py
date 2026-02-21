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

def time_to_seconds(time_str):
    """ '1:22.7' のようなタイム文字列を秒に変換 """
    if not isinstance(time_str, str) or ':' not in time_str:
        return np.nan
    try:
        m, s = time_str.split(':')
        return int(m) * 60 + float(s)
    except:
        return np.nan

def extract_first_corner(text):
    """ '－－⑩⑩' のような文字列から最初の通過順位(数字)を抽出 """
    norm = unicodedata.normalize('NFKC', text)
    matches = re.findall(r'\d+', norm)
    if matches:
        return int(matches[0])
    return 7

def calculate_early_pace_speed(row):
    """ 前半(テン)の絶対スピード(m/s)を計算し、馬場・コース補正をかける """
    if pd.isna(row['time_sec']) or pd.isna(row['f3_time']):
        return np.nan
    
    early_time = row['time_sec'] - row['f3_time']
    early_dist = row['distance'] - 600
    if early_dist <= 0 or early_time <= 0:
        return np.nan
    
    # 基準となる秒速 (m/s)
    raw_speed = early_dist / early_time
    
    # --- 馬場状態による補正 ---
    condition_mod = 0.0
    if row['track_type'] == "芝":
        if row['track_condition'] in ["重", "不良"]: condition_mod = +0.15 # タフな馬場で出した時計は価値が高い
        elif row['track_condition'] == "稍": condition_mod = +0.05
    elif row['track_type'] == "ダート":
        if row['track_condition'] in ["重", "不良"]: condition_mod = -0.15 # 足抜きが良く時計が出やすい分を割り引く
        elif row['track_condition'] == "稍": condition_mod = -0.05

    # --- コース形態（芝スタートダート等）による補正 ---
    course_mod = 0.0
    turf_start_dirt = [("東京", 1600), ("中山", 1200), ("阪神", 1400), ("京都", 1400), ("新潟", 1200)]
    if row['track_type'] == "ダート" and (row['venue'], row['distance']) in turf_start_dirt:
        course_mod = -0.2 # 芝スタートで加速がつきやすかった分を割り引く

    return raw_speed + condition_mod + course_mod

def extract_jockey_target_position(past_races_df: pd.DataFrame, current_venue: str) -> float:
    """ 同競馬場での成功体験（人気以上の着順or1着）を優先して狙う位置を抽出 """
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
        return 7.0 
    
    past_df['early_speed'] = past_df.apply(calculate_early_pace_speed, axis=1)
    max_speed = past_df['early_speed'].max()
    speed_advantage = 0.0
    if not pd.isna(max_speed):
        speed_advantage = (16.5 - max_speed) * 2.0 

    jockey_target = extract_jockey_target_position(past_df, current_venue)
    base_position = (jockey_target * 0.7) + speed_advantage
    
    last_race = past_df.iloc[0]
    weight_modifier = (horse['current_weight'] - last_race['weight']) * 0.25
    
    base_mod = (horse['horse_number'] - 1) * 0.05 
    outside_adv_courses = [("中山", 1200, "ダート"), ("東京", 1600, "ダート"), ("阪神", 1400, "ダート"), ("京都", 1400, "ダート")]
    if (current_venue, current_dist, current_track) in outside_adv_courses:
        base_mod = (total_horses - horse['horse_number']) * 0.05 - 0.4
    
    final_score = base_position + weight_modifier + base_mod
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

# ==========================================
# 2. 競馬ラボ（縦型馬柱）スクレイピングロジック
# ==========================================
def fetch_real_data(race_id: str):
    url = f"https://www.keibalab.jp/db/race/{race_id}/umabashira.html"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8' 
        time.sleep(1) 
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 開催情報の取得
        p_about = soup.select_one('p[itemprop="about"]')
        current_venue = "東京"
        if p_about:
            venue_m = re.search(r'(東京|中山|京都|阪神|中京|新潟|福島|小倉|札幌|函館)', p_about.text)
            if venue_m: current_venue = venue_m.group(1)

        course_li = soup.select('ul.classCourseSyokin li')
        current_track = "芝"
        current_dist = 1600
        if len(course_li) > 1:
            course_text = course_li[1].text
            current_track = "ダート" if "ダ" in course_text else "芝"
            dist_m = re.search(r'\d+', course_text)
            if dist_m: current_dist = int(dist_m.group(0))

        # 馬柱テーブルの解析
        tr_umaban = soup.select_one('tr.umaban')
        tr_horseName = soup.select_one('tr.horseName')
        trs_seirei = soup.select('tr.seirei')
        tr_batai = trs_seirei[2] if len(trs_seirei) > 2 else None
        
        trs_zensou = []
        for i in range(1, 6):
            tr = soup.select_one(f'tr.zensou{i}')
            if tr: trs_zensou.append(tr)

        if not tr_umaban or not tr_horseName:
            return None, current_dist, current_venue, current_track, "出馬表データが見つかりません。"

        horses_data = []
        cols = tr_umaban.find_all(['td', 'th'])
        
        # 右から左（内枠）へ配置されているデータをパース（tdのインデックス2〜最後の手前まで）
        for i in range(2, len(cols) - 1):
            h_num_text = cols[i].text.strip()
            if not h_num_text.isdigit(): continue
            horse_num = int(h_num_text)
            
            horse_name_elem = tr_horseName.find_all(['td', 'th'])[i].select_one('.bamei')
            horse_name = horse_name_elem.text.strip() if horse_name_elem else "不明"
            
            batai_text = tr_batai.find_all(['td', 'th'])[i].text.strip() if tr_batai else ""
            weight_m = re.search(r'^(\d{3})', batai_text)
            current_weight = float(weight_m.group(1)) if weight_m else 480.0
            
            past_races = []
            for tr_z in trs_zensou:
                td_z = tr_z.find_all(['td', 'th'])[i]
                if not td_z.select_one('.zensouTable'): continue
                
                # 競馬場・コース・距離・馬場
                li_elements = td_z.select('ul.daybaba li')
                if len(li_elements) < 3: continue
                
                p_venue_m = re.search(r'(東京|中山|京都|阪神|中京|新潟|福島|小倉|札幌|函館)', li_elements[0].text)
                p_venue = p_venue_m.group(1) if p_venue_m else current_venue
                
                p_track_m = re.search(r'(芝|ダ)', li_elements[2].text)
                p_track = "ダート" if p_track_m and p_track_m.group(1) == "ダ" else "芝"
                
                p_dist_m = re.search(r'\d+', li_elements[2].text)
                p_dist = int(p_dist_m.group(0)) * 100 if p_dist_m else current_dist
                
                cond_m = re.search(r'(良|稍|重|不)', li_elements[2].text)
                p_cond = cond_m.group(1) if cond_m else "良"
                if p_cond == "不": p_cond = "不良"
                
                # 着順
                cyaku_m = td_z.select_one('.cyakuJun')
                finish_pos = int(cyaku_m.text) if cyaku_m and cyaku_m.text.isdigit() else 5
                
                # タイム・人気・上がり3F
                std11_tds = td_z.select('tr:nth-of-type(3) td')
                time_text = ""
                f3_time = np.nan
                popularity = 5
                
                if std11_tds:
                    t_text = std11_tds[0].text
                    pop_m = re.search(r'(\d+)人', t_text)
                    popularity = int(pop_m.group(1)) if pop_m else 5
                    
                    time_m = re.search(r'(\d+:\d{2}\.\d+)', t_text)
                    time_text = time_m.group(1) if time_m else ""
                    
                    f3_span = std11_tds[0].select_one('span[class^="bgRise"]')
                    if f3_span:
                        try: f3_time = float(f3_span.text.strip())
                        except: pass
                
                # 位置取り
                pos_td = td_z.select_one('.zensou')
                first_corner = extract_first_corner(pos_td.text) if pos_td else 7
                
                past_races.append({
                    'venue': p_venue, 'track_type': p_track, 'distance': p_dist,
                    'track_condition': p_cond, 'finish_position': finish_pos, 'popularity': popularity,
                    'time_sec': time_to_seconds(time_text), 'f3_time': f3_time,
                    'first_corner_pos': first_corner, 'weight': current_weight
                })

            horses_data.append({
                'horse_number': horse_num, 'horse_name': horse_name,
                'current_weight': current_weight, 'past_races': past_races,
                'synergy': "", 'condition_mod': 0.0, 'special_flag': ""
            })

        if not horses_data: return None, 1600, "", "芝", "馬データが取得できませんでした。"
        
        # 馬番順にソート (HTMLは外枠から並んでいるため)
        horses_data = sorted(horses_data, key=lambda x: x['horse_number'])
        return horses_data, current_dist, current_venue, current_track, None
        
    except Exception as e:
        return None, 1600, "", "芝", f"エラー: {e}\n{traceback.format_exc()}"


# ==========================================
# 3. スマホ対応UI
# ==========================================
st.set_page_config(page_title="AI競馬展開予想", page_icon="🏇", layout="centered")

st.title("🏇 AI競馬展開予想 (ペース補正版)")
st.markdown("競馬ラボの出馬表から絶対ペース・馬場補正を計算し、勝負気配を読み取ります。")

with st.container(border=True):
    st.subheader("⚙️ レース設定")
    base_url_input = st.text_input("🔗 競馬ラボのレースURL", value="https://www.keibalab.jp/db/race/202602210910/")
    
    col1, col2 = st.columns(2)
    with col1:
        execute_btn = st.button("🚀 このレースを予想", type="primary", use_container_width=True)

if execute_btn:
    match = re.search(r'\d{12}', base_url_input)
    if not match:
        st.error("有効な競馬ラボのレースIDが見つかりません。")
        st.stop()
        
    target_race_id = match.group()
    
    with st.spinner("出馬表と過去走データを解析中..."):
        horses, current_dist, current_venue, current_track, error_msg = fetch_real_data(target_race_id)
        
        if error_msg:
            st.warning(error_msg)
            st.stop()
            
        total_horses = len(horses)
        
        for horse in horses:
            horse['score'] = calculate_pace_score(horse, current_dist, current_venue, current_track, total_horses)
            
        sorted_horses = sorted(horses, key=lambda x: x['score'])
        formation_text = format_formation(sorted_horses)

        st.info(f"📏 条件: **{current_venue} {current_track}{current_dist}m** ({total_horses}頭立て)")
        
        st.markdown(f"<h4 style='text-align: center; letter-spacing: 2px;'>◀(進行方向)</h4>", unsafe_allow_html=True)
        st.markdown(f"<h3 style='text-align: center; color: #FF4B4B;'>{formation_text}</h3>", unsafe_allow_html=True)
        
        st.markdown("---")
        
        with st.expander("📊 詳細スコアを見る (低いほど前に行ける)"):
            df_result = pd.DataFrame([{
                "馬番": h['horse_number'],
                "馬名": h['horse_name'],
                "スコア": round(h['score'], 2),
            } for h in sorted_horses])
            st.dataframe(df_result, use_container_width=True, hide_index=True)
