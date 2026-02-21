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
        return 7.0 # データがない場合は中団(7番手)をデフォルト値に

    # 1着 または 人気より着順が上の場合を「成功体験」とする
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
    
    # ① 距離変動の補正 (前走距離 - 今回距離) / 100 * 0.5
    dist_diff = last_race['distance'] - current_dist
    dist_modifier = -(dist_diff / 100) * 0.5 
    
    # ② 斤量変動の補正 (今回斤量 - 前走斤量) * 0.5
    weight_modifier = (horse['current_weight'] - last_race['weight']) * 0.5
    
    # ③ 地方競馬補正 (前走が地方なら、中央では位置を下げやすい)
    local_modifier = 2.0 if last_race['is_local'] else 0.0
    
    final_score = base_position + dist_modifier + weight_modifier + local_modifier
    return max(1.0, min(18.0, final_score))

def generate_short_comment(sorted_horses):
    """展開順に基づく短評の自動生成"""
    if len(sorted_horses) < 2:
        return "出走馬データが不足しているため、展開予想を生成できません。"
        
    leaders = sorted_horses[:2]
    chasers = sorted_horses[2:6]
    
    comment = f"ハナを主張するのはスコア最上位の{leaders[0]['horse_name']}か。"
    if leaders[1]['score'] - leaders[0]['score'] < 1.0:
        comment += f"{leaders[1]['horse_name']}も徹底先行の構えで、テンの入りは早くなりそう。"
    else:
        comment += f"単騎逃げの形になりそうで、ペースは落ち着く可能性が高い。"
        
    if len(chasers) >= 2:
        comment += f"好位には{chasers[0]['horse_name']}、{chasers[1]['horse_name']}あたりが続き、距離や斤量の恩恵を活かして前を伺う展開。"
    return comment

# ==========================================
# 2. スクレイピングロジック
# ==========================================

def fetch_real_data(race_id: str, current_dist: int) -> list:
    """netkeibaから出馬表と過去走データを取得する"""
    url = f"https://race.netkeiba.com/race/shutuba_past.html?race_id={race_id}&rf=shutuba_submenu"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'EUC-JP'
        time.sleep(1)
        
        # HTML内にテーブルが存在するか確認
        if "<table" not in response.text:
            st.error("指定されたページにデータが見つかりませんでした。レースIDが正しいか確認してください。")
            return []
            
        dfs = pd.read_html(response.text)
        df_main = dfs[0]
        
        # netkeibaの表はマルチインデックス（見出しが多段）になることがあるため平坦化
        if isinstance(df_main.columns, pd.MultiIndex):
            df_main.columns = [f"{col[0]}_{col[1]}" if col[0] != col[1] else col[0] for col in df_main.columns.values]

        horses_data = []
        
        for index, row in df_main.iterrows():
            try:
                # 馬番がない行（ヘッダー等のゴミデータ）はスキップ
                if pd.isna(row.iloc[0]): 
                    continue
                    
                # 馬番の抽出
                horse_num_match = re.search(r'\d+', str(row.iloc[0]))
                if not horse_num_match:
                    continue
                horse_number = int(horse_num_match.group())
                
                horse_name = str(row.iloc[3]).strip()
                
                # 今回斤量の抽出
                weight_match = re.findall(r'\d+\.\d+|\d+', str(row.iloc[5]))
                current_weight = float(weight_match[0]) if weight_match else 55.0
                
                past_races = []
                # 過去5走の抽出（列インデックスのズレ対策として、エラーが起きても止まらないように処理）
                for past_idx in range(5):
                    # ※ここの列番号(10)は実際のnetkeibaの仕様に合わせて微調整が必要になる箇所です
                    col_offset = 10 + (past_idx * 5)
                    
                    try:
                        if col_offset + 4 >= len(row) or pd.isna(row.iloc[col_offset]):
                            continue
                            
                        # 着順と人気
                        finish_match = re.findall(r'\d+', str(row.iloc[col_offset]))
                        pop_match = re.findall(r'\d+', str(row.iloc[col_offset+1]))
                        if not finish_match or not pop_match:
                            continue
                            
                        finish_pos = int(finish_match[0])
                        popularity = int(pop_match[0])
                        
                        # コーナー通過順
                        corner_str = str(row.iloc[col_offset+2])
                        first_corner = int(re.findall(r'\d+', corner_str)[0]) if re.findall(r'\d+', corner_str) else 7
                        
                        # 距離と地方判定
                        course_info = str(row.iloc[col_offset+3])
                        dist_match = re.findall(r'\d+', course_info)
                        distance = int(dist_match[0]) if dist_match else current_dist
                        is_local = any(loc in course_info for loc in ["名", "川", "船", "浦", "大", "盛", "水", "園", "高", "佐"])
                        
                        # 前走斤量
                        past_weight_match = re.findall(r'\d+\.\d+|\d+', str(row.iloc[col_offset+4]))
                        past_weight = float(past_weight_match[0]) if past_weight_match else current_weight
                        
                        past_races.append({
                            'finish_position': finish_pos,
                            'popularity': popularity,
                            'first_corner_pos': first_corner,
                            'distance': distance,
                            'weight': past_weight,
                            'is_local': is_local
                        })
                    except Exception:
                        pass # 1つの過去走のパースに失敗しても、他の走のデータ取得は続ける
                
                horses_data.append({
                    'horse_number': horse_number,
                    'horse_name': horse_name,
                    'current_weight': current_weight,
                    'past_races': past_races
                })
                
            except Exception as row_error:
                # 1頭の馬の処理でエラーが起きても全体を止めない
                continue

        return horses_data

    except Exception as e:
        # ここが最も重要：スクレイピング全体が失敗した際に画面にエラー詳細を出す
        st.error(f"データの取得・解析中にエラーが発生しました: {e}")
        with st.expander("エラーの詳細（開発者用）"):
            st.code(traceback.format_exc())
        return []

# ==========================================
# 3. Streamlit UI
# ==========================================

st.set_page_config(page_title="AI競馬展開予想アプリ", layout="wide")

st.title("🏇 AI競馬展開予想アプリ")
st.markdown("近5走のデータ、距離増減、斤量、騎手の成功体験バイアスから隊列を予測します。")

# サイドバー: レース条件の入力
st.sidebar.header("レース条件設定")
race_id_input = st.sidebar.text_input("レースID (netkeibaのURL)", value="202605010811")
distance_input = st.sidebar.number_input("今回の距離 (m)", min_value=1000, max_value=3600, value=1600, step=100)

if st.sidebar.button("予想を実行する", type="primary"):
    with st.spinner("netkeibaから出馬表データを取得・解析中..."):
        
        # 1. データ取得
        horses = fetch_real_data(race_id_input, distance_input)
        
        # データが空だった場合（エラー時）はここで処理をストップ
        if not horses:
            st.warning("出馬表データを抽出できませんでした。上に表示されている赤いエラー詳細を確認してください。")
            st.stop()
            
        # 2. スコアの計算
        for horse in horses:
            horse['score'] = calculate_pace_score(horse, distance_input)
            
        # 3. スコア順にソート
        sorted_horses = sorted(horses, key=lambda x: x['score'])
        
        # 4. 隊列テキストの生成
        formation_groups = []
        for i in range(0, len(sorted_horses), 4):
            group = "".join([f"[{h['horse_number']}]" for h in sorted_horses[i:i+4]])
            formation_groups.append(group)
        
        formation_text = " ◀(進行方向)  " + "  -  ".join(formation_groups)
        
        # 5. 短評の生成
        comment = generate_short_comment(sorted_horses)

        # 結果の描画
        st.success("解析が完了しました！")
        
        st.subheader("🏁 予想隊列")
        st.info(formation_text)
        
        st.subheader("📝 展開短評")
        st.write(comment)
        
        st.subheader("📊 各馬のポジショニングスコア詳細 (値が小さいほど前)")
        
        # テーブル表示用にデータ整形
        df_result = pd.DataFrame([{
            "馬番": h['horse_number'],
            "馬名": h['horse_name'],
            "ポジションスコア": round(h['score'], 2),
            "今回斤量": h['current_weight'],
            "有効過去走データ数": len(h['past_races'])
        } for h in sorted_horses])
        
        st.dataframe(df_result, use_container_width=True)
