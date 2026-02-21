import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import re

def fetch_real_data(race_id: str, current_dist: int) -> list:
    """
    netkeibaの出馬表（過去走）ページからデータをスクレイピングして
    アプリ用のリスト形式に変換する関数
    """
    # ユーザーが提示したURL構造（?race_id= に修正しています）
    url = f"https://race.netkeiba.com/race/shutuba_past.html?race_id={race_id}&rf=shutuba_submenu"
    
    # サーバーへの配慮とブロック回避のためのヘッダー設定
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    response = requests.get(url, headers=headers)
    # netkeiba特有の文字化け対策（EUC-JP）
    response.encoding = 'EUC-JP' 
    
    # お作法としての1秒スリープ（連続で12レース取得する際の安全網）
    time.sleep(1)
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # 馬番と馬名の取得（クラス名は実際のDOMに合わせる必要があります）
    # ※以下は一般的なnetkeibaの構造に基づくパース例です
    horses_data = []
    
    try:
        # テーブル全体を取得 (pandasのread_htmlは複雑な表をリストで返すため非常に便利)
        dfs = pd.read_html(response.text)
        
        # 出馬表メインのテーブル（通常はページ内の最初の大きなテーブル）
        df_main = dfs[0] 
        
        # ※ここから先は取得したDataFrameの列インデックスに合わせてパースします。
        # 今回はアプリの挙動に合わせて、抽出ロジックの「骨組み」を作成しています。
        # 実際のテーブル構造（列名がマルチインデックス等）に合わせて列番号(iloc)の調整が必要です。
        
        for index, row in df_main.iterrows():
            # 欠損値（空行）をスキップ
            if pd.isna(row.iloc[0]): 
                continue
                
            horse_number = int(row.iloc[0]) # 馬番
            horse_name = str(row.iloc[3])   # 馬名
            current_weight = float(re.findall(r'\d+\.\d+|\d+', str(row.iloc[5]))[0]) # 今回斤量（文字列から数値抽出）
            
            past_races = []
            # 過去5走のデータは通常、列の後半（例: 10列目以降〜）に配置されています
            # 5走分のブロックをループ処理
            for past_idx in range(5):
                # ※下記は仮の列番号です。実際のHTMLテーブルの列に合わせてシフトさせます
                col_offset = 10 + (past_idx * 5) 
                
                try:
                    # データが存在しない場合（初出走など）はスキップ
                    if pd.isna(row.iloc[col_offset]):
                        continue
                        
                    # 各項目の抽出（正規表現などを駆使してテキストから数値を抜きます）
                    finish_pos = int(re.findall(r'\d+', str(row.iloc[col_offset]))[0]) # 着順
                    popularity = int(re.findall(r'\d+', str(row.iloc[col_offset+1]))[0]) # 人気
                    
                    # 最初のコーナー位置（例: "3-3-4" という通過順の最初の数字を取る）
                    corner_str = str(row.iloc[col_offset+2])
                    first_corner = int(re.findall(r'\d+', corner_str)[0]) if re.findall(r'\d+', corner_str) else 7
                    
                    # 距離と地方/中央の判定（例: "ダ1600" "名ダ1400"）
                    course_info = str(row.iloc[col_offset+3])
                    distance = int(re.findall(r'\d+', course_info)[0])
                    is_local = "名" in course_info or "川" in course_info or "船" in course_info # 地方競馬場の頭文字で簡易判定
                    
                    past_weight = float(re.findall(r'\d+\.\d+|\d+', str(row.iloc[col_offset+4]))[0])
                    
                    past_races.append({
                        'finish_position': finish_pos,
                        'popularity': popularity,
                        'first_corner_pos': first_corner,
                        'distance': distance,
                        'weight': past_weight,
                        'is_local': is_local
                    })
                except Exception:
                    # パースエラー（競争中止など）が起きた走は無視する
                    pass
            
            horses_data.append({
                'horse_number': horse_number,
                'horse_name': horse_name,
                'current_weight': current_weight,
                'past_races': past_races
            })
            
    except Exception as e:
        print(f"スクレイピングエラー: {e}")
        # エラー時は空リストを返すなど、アプリが落ちない処理
        return []

    return horses_data
