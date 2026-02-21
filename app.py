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
    """成功体験バイアス（騎手心理）に基づく目標ポジションを算出"""
    if past_races_df.empty:
        return 7.0 

    is_success = (past_races_df['finish_position'] == 1) | \
                 (past_races_df['popularity'] > past_races_df['finish_position'])
    success_races = past_races_df[is_success]
    
    if not success_races.empty:
        upset_score = success_races['popularity'] - success_races['finish_position']
        win_bonus = np.where(success_races['finish_position'] == 1, 10, 0)
        success_score = upset_score + win_bonus
        best_memory_idx = success_score.idxmax()
        return float(past_races_df.loc[best_memory_idx, 'first_corner_pos'])
    else:
        return float(past_races_df['first_corner_pos'].mean())

def calculate_pace_score(horse, current_dist):
    """各馬の予想ポジションスコアを算出（値が小さいほど前に行く）"""
    past_df = pd.DataFrame(horse['past_races'])
    base_position = extract_jockey_target_position(past_df)
    
    if past_df.empty:
        return base_position
        
    last_race = past_df.iloc[0]
    
    # ① 距離変動の補正
    # 今回が前走より短ければ前を取りにくい(+補正)、前走より長ければ前を取りやすい(-補正)
    dist_diff = last_race['distance'] - current_dist
    dist_modifier = (dist_diff / 100.0) * 0.5 
    
    # ② 斤量変動の補正
    # 騎手の斤量が減るといつもより前にいきやすい(-補正)
    weight_modifier = (horse['current_weight'] - last_race['weight']) * 0.5
    
    # ③ 地方競馬補正
    # 地方競馬場で走っているときは、いつもよりかなり前の位置を取りやすい(-補正)
    local_modifier = -2.0 if last_race['is_local'] else 0.0
    
    final_score = base_position + dist_modifier + weight_modifier + local_modifier
    return max(1.0, min(18.0, final_score))

def format_formation(sorted_horses):
    """展開のフォーマット：(⑥⑧) ⑨④③②① ⑤⑦"""
    leaders, chasers, mid, backs = [], [], [], []
    for h in sorted_horses:
        num_str = chr(9311 + h['horse_number'])
        score = h['score']
        if score <= 4.0: leaders.append(num_str)
        elif score <= 8.0: chasers.append(num_str)
        elif score <= 13.0: mid.append(num_str)
        else: backs.append(num_str)
        
    if not leaders and sorted_horses:
        leaders.append(chr(9311 + sorted_horses[0]['horse_number']))
        if chasers and chasers[0] == leaders[0]:
            chasers.pop(0)
            
    parts = []
    if leaders: parts.append(f"({''.join(leaders)})")
    if chasers: parts.append("".join(chasers))
    if mid: parts.append("".join(mid))
    if backs: parts.append("".join(backs))
    return " ".join(parts)

def generate_short_comment(sorted_horses):
    """展開順に基づく短評の自動生成"""
    if len(sorted_horses) < 2:
        return "出走馬データが不足しているため、展開予想を生成できません。"
        
    leaders = [h for h in sorted_horses if h['score'] <= 4.0]
    if not leaders:
        leaders = [sorted_horses[0]]
        if len(sorted_horses) > 1 and sorted_horses[1]['score'] - sorted_horses[0]['score'] < 1.0:
            leaders.append(sorted_horses[1])
            
    leader_nums = "と".join([chr(9311 + h['horse_number']) for h in leaders])
    
    if len(leaders) >= 3:
        return f"ハイペース。{leader_nums}が激しく逃げを争う展開で、ペースは早くなりそう。"
    elif len(leaders) == 2:
        return f"平均ペース。{leader_nums}が逃げたがるがそれ以外は不在。"
    else:
        return f"スローペース。{leader_nums}の単騎逃げの形になりそうで、ペースは落ち着く可能性が高い。"

# ==========================================
# 2. Yahoo!スポーツ競馬・BeautifulSoup解析ロジック
# ==========================================

def fetch_real_data(race_id: str):
    """Yahoo!競馬の出馬表（詳細）ページをパースしてデータを取得する"""
    url = f"https://sports.yahoo.co.jp/keiba/race/denma/{race_id}?detail=1"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8' # Yahoo競馬はUTF-8
        time.sleep(1) # 連続アクセス時のマナー
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 出馬表テーブルが存在するか確認 (左側の固定テーブル)
        if not soup.select_one('#denma_latest'):
            return None, 1600, "出馬表データが見つかりませんでした。URLが間違っているか、出馬表が未確定です。"
        
        # レース距離を抽出
        current_dist = 1600 
        status_div = soup.select_one('.hr-predictRaceInfo__status')
        if status_div:
            dist_match = re.search(r'(\d{4})m', status_div.text)
            if dist_match:
                current_dist = int(dist_match.group(1))

        horses_data = []
        
        # Yahoo競馬の馬柱は左列（馬番など）と右列（過去走など）で別テーブルになっているため、zipで同時に回す
        latest_trs = soup.select('#denma_latest tbody tr')
        past_trs = soup.select('#denma_past tbody tr')

        for tr_latest, tr_past in zip(latest_trs, past_trs):
            # ===== 左側テーブルからの情報抽出 =====
            # 馬番
            num_elem = tr_latest.select_one('.hr-denma__number')
            if not num_elem: continue
            horse_num = int(num_elem.text.strip())

            # 馬名
            name_elem = tr_latest.select_one('.hr-denma__horse a')
            horse_name = name_elem.text.strip() if name_elem else "不明"

            # ===== 右側テーブルからの情報抽出 =====
            # 今回斤量 (hr-tableScroll__data--name の最後のpタグに入っている 例: 56.5)
            info_td = tr_past.select_one('.hr-tableScroll__data--name')
            current_weight = 55.0
            if info_td:
                p_tags = info_td.find_all('p')
                if p_tags:
                    try:
                        current_weight = float(p_tags[-1].text.strip())
                    except ValueError:
                        pass

            past_races = []
            past_tds = tr_past.select('.hr-tableScroll__data--race')

            for td in past_tds:
                # 着順
                arr_elem = td.select_one('.hr-denma__arrival')
                if not arr_elem:
                    continue # 着順が無い場合は未出走や取消などなのでスキップ
                    
                try:
                    finish_pos = int(re.search(r'\d+', arr_elem.text).group())
                except:
                    continue # 「中止」などの文字列エラー回避

                txt = td.text

                # 人気 (例: 2人気)
                pop_match = re.search(r'\((\d+)人気\)', txt)
                popularity = int(pop_match.group(1)) if pop_match else 7

                # コーナー通過順 (例: 03-03-03-03 の最初の数字)
                pass_elem = td.select_one('.hr-denma__passing')
                first_corner = 7
                if pass_elem:
                    p_match = re.search(r'^(\d+)', pass_elem.text.strip())
                    if p_match:
                        first_corner = int(p_match.group(1))

                # 距離
                dist_match_past = re.search(r'(\d{4})m', txt)
                distance = int(dist_match_past.group(1)) if dist_match_past else current_dist

                # 地方競馬判定
                is_local = any(loc in txt for loc in ["川崎", "大井", "船橋", "浦和", "門別", "盛岡", "水沢", "園田", "姫路", "高知", "佐賀", "名古屋", "笠松", "金沢", "帯広"])

                # 過去斤量 (例: 太宰 啓介(56.5))
                past_j_elem = td.select_one('.hr-denma__jockey')
                past_weight = current_weight
                if past_j_elem:
                    w_match = re.search(r'\((\d{2}(?:\.\d)?)\)', past_j_elem.text)
                    if w_match:
                        past_weight = float(w_match.group(1))

                past_races.append({
                    'finish_position': finish_pos,
                    'popularity': popularity,
                    'first_corner_pos': first_corner,
                    'distance': distance,
                    'weight': past_weight,
                    'is_local': is_local
                })

            horses_data.append({
                'horse_number': horse_num,
                'horse_name': horse_name,
                'current_weight': current_weight,
                'past_races': past_races
            })

        if not horses_data:
            return None, current_dist, "馬柱データが見つかりませんでした。出馬表が未確定です。"
            
        return horses_data, current_dist, None

    except Exception as e:
        error_msg = traceback.format_exc()
        return None, 1600, f"スクレイピング中にエラーが発生しました: {e}\n{error_msg}"

# ==========================================
# 3. Streamlit UI
# ==========================================

st.set_page_config(page_title="AI競馬展開予想 (Yahoo!競馬版)", page_icon="🏇", layout="wide")

st.title("🏇 AI競馬展開予想 (複数レース一括処理)")
st.markdown("Yahoo!競馬のデータから、距離増減、斤量、騎手の成功体験バイアスを元に隊列を予測します。")

# --- サイドバーUI ---
st.sidebar.header("レース条件設定")
st.sidebar.markdown("例: `https://sports.yahoo.co.jp/keiba/race/denma/2605010711?detail=1`")

# 1. 基準となるURLの入力
base_url_input = st.sidebar.text_input("Yahoo!競馬のURL (どれか1レースでOK)", value="https://sports.yahoo.co.jp/keiba/race/denma/2605010711?detail=1")

# 2. レース番号選択 (複数選択可能)
selected_races = st.sidebar.multiselect(
    "展開を予想したいレース番号 (複数選択可)", 
    options=list(range(1, 13)), 
    default=[11], 
    format_func=lambda x: f"{x}R"
)

if st.sidebar.button("予想を実行する", type="primary"):
    # URLから10桁のベースID（最初の8桁: 年/場/回/日）を抽出
    match = re.search(r'\d{10}', base_url_input)
    if not match:
        st.error("有効なYahoo!競馬のレースID(10桁)が見つかりません。URLを確認してください。")
        st.stop()
        
    base_id = match.group()[:8] # 例: 26050107
    
    if not selected_races:
        st.warning("レース番号を1つ以上選択してください。")
        st.stop()
        
    for race_num in sorted(selected_races):
        # レースIDの生成 (ベースID + レース番号2桁)
        target_race_id = f"{base_id}{race_num:02d}"
        target_url = f"https://sports.yahoo.co.jp/keiba/race/denma/{target_race_id}?detail=1"
        
        st.header(f"🏁 {race_num}R (距離自動取得)")
        st.caption(f"参照URL: {target_url}")
        
        with st.spinner(f"{race_num}Rのデータを取得・解析中..."):
            horses, current_dist, error_msg = fetch_real_data(target_race_id)
            
            if error_msg:
                st.error(f"{race_num}Rのデータ取得に失敗しました。")
                with st.expander("エラー詳細"):
                    st.code(error_msg)
                st.divider()
                continue
                
            st.info(f"📏 判定された今回のレース距離: **{current_dist}m**")
                
            # スコア計算
            for horse in horses:
                horse['score'] = calculate_pace_score(horse, current_dist)
                
            # スコア順（前に行く順）にソート
            sorted_horses = sorted(horses, key=lambda x: x['score'])
            
            # 隊列テキストの生成
            formation_text = format_formation(sorted_horses)
            
            # 短評の生成
            comment = generate_short_comment(sorted_horses)

            # 結果の描画
            st.success("展開予想")
            st.markdown(f"**展開：{formation_text}**")
            st.markdown(f"**短評：{comment}**")
            
            with st.expander(f"{race_num}R 各馬のポジショニングスコア詳細"):
                df_result = pd.DataFrame([{
                    "馬番": h['horse_number'],
                    "馬名": h['horse_name'],
                    "ポジションスコア": round(h['score'], 2),
                    "今回斤量": h['current_weight'],
                    "有効過去走データ数": len(h['past_races'])
                } for h in sorted_horses])
                st.dataframe(df_result, use_container_width=True)
                
        st.divider()
