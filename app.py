# ==========================================
# 1. 展開予想のコアロジック
# ==========================================

def extract_jockey_target_position(past_races_df: pd.DataFrame) -> float:
    """成功体験バイアス（騎手心理＋同コース適性）に基づく目標ポジションを算出"""
    if past_races_df.empty:
        return 7.0 

    is_success = (past_races_df['finish_position'] == 1) | \
                 (past_races_df['popularity'] > past_races_df['finish_position'])
    success_races = past_races_df[is_success]
    
    if not success_races.empty:
        upset_score = success_races['popularity'] - success_races['finish_position']
        win_bonus = np.where(success_races['finish_position'] == 1, 10, 0)
        
        # 【NEW】今回と同じ競馬場での成功体験には強力なボーナスを加算
        same_venue_bonus = np.where(success_races.get('is_same_venue', False), 8, 0)
        
        success_score = upset_score + win_bonus + same_venue_bonus
        best_memory_idx = success_score.idxmax()
        return float(past_races_df.loc[best_memory_idx, 'first_corner_pos'])
    else:
        return float(past_races_df['first_corner_pos'].mean())

def calculate_pace_score(horse, current_dist):
    """各馬の予想ポジションスコアを算出（プロ仕様の補正版）"""
    past_df = pd.DataFrame(horse['past_races'])
    
    if past_df.empty:
        return 7.0 
        
    # --- 1. ベース先行力の算出（馬の実力60% + 騎手心理40%） ---
    recent_3_avg = past_df.head(3)['first_corner_pos'].mean() # 近3走の平均位置
    jockey_target = extract_jockey_target_position(past_df)   # 成功体験バイアス
    base_position = (recent_3_avg * 0.6) + (jockey_target * 0.4)
    
    last_race = past_df.iloc[0]
    
    # --- 2. 距離変動の補正 (キャップ制御) ---
    dist_diff = last_race['distance'] - current_dist
    clipped_diff = max(-400, min(400, dist_diff))
    dist_modifier = (clipped_diff / 100.0) * 0.2 
    
    # --- 3. 斤量変動の補正 (マイルド化) ---
    weight_modifier = (horse['current_weight'] - last_race['weight']) * 0.25
    
    # --- 4. 地方競馬補正 ---
    local_modifier = -1.0 if last_race['is_local'] else 0.0
    
    # --- 5. 枠順補正（外枠ほど前に行きにくいロスを加算） ---
    frame_modifier = (horse['horse_number'] - 1) * 0.05

    final_score = base_position + dist_modifier + weight_modifier + local_modifier + frame_modifier
    
    return max(1.0, min(18.0, final_score))

def format_formation(sorted_horses):
    """展開のフォーマット：相対評価で隊列を組む"""
    if not sorted_horses:
        return ""
        
    leaders, chasers, mid, backs = [], [], [], []
    top_score = sorted_horses[0]['score']
    
    for h in sorted_horses:
        num_str = chr(9311 + h['horse_number']) 
        score = h['score']
        
        if score <= top_score + 1.2 and len(leaders) < 3:
            leaders.append(num_str)
        elif score <= top_score + 4.5:
            chasers.append(num_str)
        elif score <= top_score + 9.5:
            mid.append(num_str)
        else:
            backs.append(num_str)
            
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
    """相対的なスコア差から展開の起伏を読む短評生成"""
    if len(sorted_horses) < 2:
        return "出走馬データが不足しているため、展開予想を生成できません。"
        
    top_score = sorted_horses[0]['score']
    leaders = [h for h in sorted_horses if h['score'] <= top_score + 1.2][:3]
    leader_nums = "と".join([chr(9311 + h['horse_number']) for h in leaders])
    
    gap_to_second = sorted_horses[1]['score'] - top_score
    
    if len(leaders) >= 3:
        return f"ハイペース。{leader_nums}がハナを主張し合い、テンのペースは早くなりそう。縦長の展開か。"
    elif len(leaders) == 2 and gap_to_second < 0.5:
        return f"平均ペース。{leader_nums}が並んで先行争い。隊列は比較的すんなり決まりそう。"
    elif gap_to_second >= 1.5:
        return f"スローペース。{leader_nums}が楽に単騎逃げの形を作れそう。後続は折り合い重視の展開。"
    else:
        return f"平均〜スローペース。{leader_nums}が主導権を握るが、競りかける馬はおらずペースは落ち着く可能性が高い。"

# ==========================================
# 2. Yahoo!スポーツ競馬・BeautifulSoup解析ロジック
# ==========================================

def fetch_real_data(race_id: str):
    """Yahoo!競馬の出馬表（詳細）ページをパースしてデータを取得する"""
    url = f"https://sports.yahoo.co.jp/keiba/race/denma/{race_id}?detail=1"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8' 
        time.sleep(1) 
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        if not soup.select_one('#denma_latest'):
            return None, 1600, "出馬表データが見つかりませんでした。URLが間違っているか、出馬表が未確定です。"
        
        # 【NEW】今回の競馬場名を取得（例：東京、中山）
        current_venue = ""
        venue_elem = soup.select_one('.hr-menuWhite__item--current .hr-menuWhite__text')
        if venue_elem:
            current_venue = venue_elem.text.strip()
            
        current_dist = 1600 
        status_div = soup.select_one('.hr-predictRaceInfo__status')
        if status_div:
            dist_match = re.search(r'(\d{4})m', status_div.text)
            if dist_match:
                current_dist = int(dist_match.group(1))

        horses_data = []
        latest_trs = soup.select('#denma_latest tbody tr')
        past_trs = soup.select('#denma_past tbody tr')

        for tr_latest, tr_past in zip(latest_trs, past_trs):
            num_elem = tr_latest.select_one('.hr-denma__number')
            if not num_elem: continue
            horse_num = int(num_elem.text.strip())

            name_elem = tr_latest.select_one('.hr-denma__horse a')
            horse_name = name_elem.text.strip() if name_elem else "不明"

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
                arr_elem = td.select_one('.hr-denma__arrival')
                if not arr_elem:
                    continue 
                    
                try:
                    finish_pos = int(re.search(r'\d+', arr_elem.text).group())
                except:
                    continue 

                txt = td.text

                # 人気
                pop_match = re.search(r'\((\d+)人気\)', txt)
                popularity = int(pop_match.group(1)) if pop_match else 7

                # コーナー通過順
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

                # 【NEW】過去の競馬場が今回と同じかどうか判定
                is_same_venue = False
                date_spans = td.select('.hr-denma__date span')
                if len(date_spans) >= 2:
                    past_venue = date_spans[1].text.strip()
                    if current_venue and past_venue in current_venue:
                        is_same_venue = True
                elif current_venue and current_venue in txt:
                    is_same_venue = True

                # 過去斤量
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
                    'is_local': is_local,
                    'is_same_venue': is_same_venue # 今回の競馬場と同じならTrue
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
st.markdown("Yahoo!競馬のデータから、距離増減、斤量、枠順、騎手の成功体験バイアス（同コース適性含む）を元に隊列を予測します。")

# --- サイドバーUI ---
st.sidebar.header("レース条件設定")
st.sidebar.markdown("例: `https://sports.yahoo.co.jp/keiba/race/denma/2605010711?detail=1`")

base_url_input = st.sidebar.text_input("Yahoo!競馬のURL (どれか1レースでOK)", value="https://sports.yahoo.co.jp/keiba/race/denma/2605010711?detail=1")

selected_races = st.sidebar.multiselect(
    "展開を予想したいレース番号 (複数選択可)", 
    options=list(range(1, 13)), 
    default=[11], 
    format_func=lambda x: f"{x}R"
)

if st.sidebar.button("予想を実行する", type="primary"):
    match = re.search(r'\d{10}', base_url_input)
    if not match:
        st.error("有効なYahoo!競馬のレースID(10桁)が見つかりません。URLを確認してください。")
        st.stop()
        
    base_id = match.group()[:8] 
    
    if not selected_races:
        st.warning("レース番号を1つ以上選択してください。")
        st.stop()
        
    for race_num in sorted(selected_races):
        target_race_id = f"{base_id}{race_num:02d}"
        target_url = f"https://sports.yahoo.co.jp/keiba/race/denma/{target_race_id}?detail=1"
        
        st.header(f"🏁 {race_num}R (距離・コース自動取得)")
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
                
            for horse in horses:
                horse['score'] = calculate_pace_score(horse, current_dist)
                
            sorted_horses = sorted(horses, key=lambda x: x['score'])
            formation_text = format_formation(sorted_horses)
            comment = generate_short_comment(sorted_horses)

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
