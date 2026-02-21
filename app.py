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
# 2. 競馬ラボ・BeautifulSoupスクレイピングロジック
# ==========================================

def fetch_real_data(race_id_input: str, current_dist: int) -> list:
    """競馬ラボの馬柱ページから直接HTMLタグをパースしてデータを取得する"""
    
    # ユーザー入力からURL内の12桁の数字（レースID）を抽出
    match = re.search(r'\d{12}', race_id_input)
    if not match:
        st.error("有効なレースIDが見つかりません。競馬ラボのURLを確認してください。")
        return []
    race_id = match.group()
    
    # 競馬ラボの仕様：近走データは「馬柱（umabashira.html）」ページにまとまっている
    url = f"https://www.keibalab.jp/db/race/{race_id}/umabashira.html"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8' # 競馬ラボはUTF-8
        time.sleep(1)
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 競馬ラボの馬柱テーブルを取得
        table = soup.find('table', class_=re.compile(r'umabashira|dataTbl', re.I))
        if not table:
            st.error("馬柱テーブルが見つかりませんでした。データがまだ公開されていない可能性があります。")
            return []

        horses_data = []
        
        # テーブルの行（tr）ごとに処理
        for tr in table.find_all('tr'):
            tds = tr.find_all(['td', 'th'])
            if len(tds) < 5: continue # ヘッダー行などはスキップ
                
            # 馬名の抽出（リンク先が /db/horse/ のaタグ）
            a_tag = tr.find('a', href=re.compile(r'/db/horse/'))
            if not a_tag: continue
            horse_name = a_tag.text.strip()
            
            # 馬番の抽出（前半のtd内で1〜18の数字だけのセル）
            horse_num = None
            for td in tds[:5]:
                txt = td.text.strip()
                if txt.isdigit() and 1 <= int(txt) <= 18:
                    horse_num = int(txt) # 枠番と馬番がある場合、後ろの馬番で上書きされる
            if horse_num is None: continue
                
            # 今回斤量の抽出（55.0 のような数値）
            current_weight = 55.0
            for td in tds:
                txt = td.text.strip()
                match_weight = re.search(r'(?:5[0-9]|6[0-3]|4[8-9])\.\d', txt)
                if match_weight and "kg" not in txt and len(txt) < 20:
                    current_weight = float(match_weight.group())
                    break

            past_races = []
            
            # 過去走データは「着」「人」「m」などの文字が含まれるtdに存在する
            potential_past_tds = [td for td in tds if "走" in td.text or "m" in td.text or "着" in td.text or "人" in td.text]
            if not potential_past_tds:
                potential_past_tds = tds[-5:] # 見つからなければ後ろから5つを強制取得

            for td in potential_past_tds[:5]:
                txt = td.text.strip()
                if len(txt) < 15: continue # 「取消」やデータ無しの空セルをスキップ
                    
                try:
                    # 着順抽出（「前走」などの文字を消した直後の数字）
                    txt_clean = re.sub(r'(?:前走|\d走前)', '', txt).strip()
                    finish_match = re.search(r'^(\d{1,2})', txt_clean)
                    if not finish_match: continue
                    finish_pos = int(finish_match.group(1))

                    # 人気抽出
                    pop_match = re.search(r'(\d+)人', txt)
                    popularity = int(pop_match.group(1)) if pop_match else 7

                    # 最初のコーナー位置（競馬ラボ特有の丸囲み数字 ①〜⑱ をパース）
                    corner_match = re.search(r'([①-⑱])', txt)
                    if corner_match:
                        circle_nums = {'①':1, '②':2, '③':3, '④':4, '⑤':5, '⑥':6, '⑦':7, '⑧':8, '⑨':9, '⑩':10, '⑪':11, '⑫':12, '⑬':13, '⑭':14, '⑮':15, '⑯':16, '⑰':17, '⑱':18}
                        first_corner = circle_nums.get(corner_match.group(1), 7)
                    else:
                        first_corner = 7

                    # 距離抽出
                    dist_match = re.search(r'(?:芝|ダ|障)(\d+)m', txt)
                    distance = int(dist_match.group(1)) if dist_match else current_dist

                    # 地方競馬判定（文字列に地方競馬場の名前が含まれるか）
                    is_local = any(loc in txt for loc in ["川崎", "大井", "船橋", "浦和", "門別", "盛岡", "水沢", "園田", "姫路", "高知", "佐賀", "名古屋", "笠松", "金沢", "帯広"])

                    # 過去の斤量抽出
                    weight_matches = re.findall(r'(?:5[0-9]|6[0-3]|4[8-9])\.\d', txt)
                    past_weight = float(weight_matches[-1]) if weight_matches else current_weight

                    past_races.append({
                        'finish_position': finish_pos,
                        'popularity': popularity,
                        'first_corner_pos': first_corner,
                        'distance': distance,
                        'weight': past_weight,
                        'is_local': is_local
                    })
                except Exception:
                    pass # 1つの過去走でエラーが起きても無視して次へ
            
            horses_data.append({
                'horse_number': horse_num,
                'horse_name': horse_name,
                'current_weight': current_weight,
                'past_races': past_races
            })

        return horses_data

    except Exception as e:
        st.error(f"データの解析中にエラーが発生しました: {e}")
        with st.expander("エラーの詳細（開発者用）"):
            st.code(traceback.format_exc())
        return []

# ==========================================
# 3. Streamlit UI
# ==========================================

st.set_page_config(page_title="AI競馬展開予想アプリ", layout="wide")

st.title("🏇 AI競馬展開予想アプリ")
st.markdown("競馬ラボのデータから、距離増減、斤量、騎手の成功体験バイアスを元に隊列を予測します。")

# サイドバー: レース条件の入力
st.sidebar.header("レース条件設定")
st.sidebar.markdown("例: `https://www.keibalab.jp/db/race/202602220511/`")
race_id_input = st.sidebar.text_input("競馬ラボのURL または レースID", value="202602220511")
distance_input = st.sidebar.number_input("今回の距離 (m)", min_value=1000, max_value=3600, value=1600, step=100)

if st.sidebar.button("予想を実行する", type="primary"):
    with st.spinner("競馬ラボからデータを取得・解析中..."):
        
        horses = fetch_real_data(race_id_input, distance_input)
        
        if not horses:
            st.warning("出馬表データを抽出できませんでした。上に表示されている赤いエラー詳細を確認してください。")
            st.stop()
            
        for horse in horses:
            horse['score'] = calculate_pace_score(horse, distance_input)
            
        sorted_horses = sorted(horses, key=lambda x: x['score'])
        
        formation_groups = []
        for i in range(0, len(sorted_horses), 4):
            group = "".join([f"[{h['horse_number']}]" for h in sorted_horses[i:i+4]])
            formation_groups.append(group)
        
        formation_text = " ◀(進行方向)  " + "  -  ".join(formation_groups)
        
        comment = generate_short_comment(sorted_horses)

        st.success("解析が完了しました！")
        
        st.subheader("🏁 予想隊列")
        st.info(formation_text)
        
        st.subheader("📝 展開短評")
        st.write(comment)
        
        st.subheader("📊 各馬のポジショニングスコア詳細 (値が小さいほど前)")
        
        df_result = pd.DataFrame([{
            "馬番": h['horse_number'],
            "馬名": h['horse_name'],
            "ポジションスコア": round(h['score'], 2),
            "今回斤量": h['current_weight'],
            "有効過去走データ数": len(h['past_races'])
        } for h in sorted_horses])
        
        st.dataframe(df_result, use_container_width=True)
