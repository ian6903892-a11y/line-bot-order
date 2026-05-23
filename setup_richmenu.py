import os
import sys
import json
import requests
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
BASE_URL      = os.getenv('BASE_URL', 'http://localhost:5000')
HEADERS       = {'Authorization': f'Bearer {ACCESS_TOKEN}'}

# ── 1. 建立選單圖片 ────────────────────────────────────
W, H = 2500, 843
img  = Image.new('RGB', (W, H), '#1a1a2e')
draw = ImageDraw.Draw(img)

# 左半：深紅 → 「查看價目表」
draw.rectangle([0, 0, W//2 - 4, H], fill='#1a0a2e')
# 右半：橘紅 → 「立即下單」
draw.rectangle([W//2 + 4, 0, W, H], fill='#c0392b')
# 中間分隔線
draw.rectangle([W//2 - 4, 0, W//2 + 4, H], fill='#333')

# 嘗試載入字型（找不到就用預設）
try:
    font_big  = ImageFont.truetype('C:/Windows/Fonts/msjh.ttc', 110)
    font_small = ImageFont.truetype('C:/Windows/Fonts/msjh.ttc', 52)
except Exception:
    font_big  = ImageFont.load_default()
    font_small = font_big

# 左邊文字
draw.text((W//4, H//2 - 80), '查看價目表', font=font_big,  fill='#ffffff', anchor='mm')
draw.text((W//4, H//2 + 60), '查詢最新優惠',  font=font_small, fill='rgba(255,255,255,128)', anchor='mm')

# 右邊文字
draw.text((W*3//4, H//2 - 80), '立即下單', font=font_big,  fill='#ffffff', anchor='mm')
draw.text((W*3//4, H//2 + 60), 'VALORANT VP 儲值', font=font_small, fill='rgba(255,255,255,200)', anchor='mm')

img_path = 'richmenu.png'
img.save(img_path)
print(f'圖片建立完成：{img_path}')


# ── 2. 刪除舊的選單 ───────────────────────────────────
def delete_old_menus():
    r = requests.get('https://api.line.me/v2/bot/richmenu/list', headers=HEADERS)
    if r.status_code == 200:
        for menu in r.json().get('richmenus', []):
            mid = menu['richMenuId']
            requests.delete(f'https://api.line.me/v2/bot/richmenu/{mid}', headers=HEADERS)
            print(f'刪除舊選單：{mid}')

delete_old_menus()


# ── 3. 建立新選單 ─────────────────────────────────────
menu_config = {
    "size": {"width": W, "height": H},
    "selected": True,
    "name": "主選單",
    "chatBarText": "查看選單",
    "areas": [
        {
            "bounds": {"x": 0, "y": 0, "width": W//2, "height": H},
            "action": {"type": "uri", "uri": f"{BASE_URL}/price"}
        },
        {
            "bounds": {"x": W//2, "y": 0, "width": W//2, "height": H},
            "action": {"type": "uri", "uri": f"https://liff.line.me/{os.getenv('LIFF_ID', '2010170038-RqcUvYpB')}"}
        }
    ]
}

r = requests.post(
    'https://api.line.me/v2/bot/richmenu',
    headers={**HEADERS, 'Content-Type': 'application/json'},
    data=json.dumps(menu_config)
)

if r.status_code != 200:
    print(f'建立選單失敗：{r.text}')
    sys.exit(1)

menu_id = r.json()['richMenuId']
print(f'選單建立成功：{menu_id}')


# ── 4. 上傳圖片 ───────────────────────────────────────
with open(img_path, 'rb') as f:
    r = requests.post(
        f'https://api-data.line.me/v2/bot/richmenu/{menu_id}/content',
        headers={**HEADERS, 'Content-Type': 'image/png'},
        data=f.read()
    )

if r.status_code != 200:
    print(f'上傳圖片失敗：{r.text}')
    sys.exit(1)

print('圖片上傳成功')


# ── 5. 設為預設選單 ───────────────────────────────────
r = requests.post(
    f'https://api.line.me/v2/bot/richmenu/default/{menu_id}',
    headers=HEADERS
)

if r.status_code == 200:
    print('已設為預設選單，完成！')
else:
    print(f'設定預設失敗：{r.text}')
