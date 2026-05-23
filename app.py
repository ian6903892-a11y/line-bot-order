import os
import sys
import hashlib
import urllib.parse
import random
import string
import binascii
import time
import json
from datetime import datetime
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
from flask import Flask, request, abort, render_template, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage,
    TemplateMessage, ButtonsTemplate, URIAction
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

app = Flask(__name__)
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# ── 商品設定（自行修改）──────────────────────────────────
PRODUCTS = {
    '1': {'name': 'LOL 拳頭點數 100點',      'price': 50},
    '2': {'name': 'LOL 拳頭點數 500點',      'price': 240},
    '3': {'name': '原神 創世結晶 60個',       'price': 30},
    '4': {'name': '原神 創世結晶 330個',      'price': 150},
    '5': {'name': 'Steam 錢包 NT$100',       'price': 100},
    '6': {'name': 'Steam 錢包 NT$500',       'price': 500},
}

# ── 狀態機（記錄每個用戶的對話進度）──────────────────────
states = {}
pending_orders = {}
otp_store = {}   # {user_id: {'otp': str, 'expires': float, 'phone': str}}

# ── Google 試算表 ─────────────────────────────────────
SHEET_HEADERS = ['訂單編號', '時間', '用戶ID', '商品', 'Riot ID', '登入方式', '帳號', '密碼', '載具', '數量', '金額', '付款方式', '狀態']

def get_sheet():
    creds = Credentials.from_service_account_file(
        'google_credentials.json',
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.getenv('GOOGLE_SHEET_ID')).sheet1

def init_sheet():
    try:
        sheet = get_sheet()
        if not sheet.row_values(1):
            sheet.append_row(SHEET_HEADERS)
        print('✅ Google 試算表連線成功')
    except Exception as e:
        print(f'⚠️  Google 試算表未設定（之後再填）: {e}')

def save_order(order):
    try:
        sheet = get_sheet()
        sheet.append_row([
            order['order_id'],
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            order['user_id'],
            order['product'],
            order['game_account'],
            order.get('login_type', ''),
            order.get('login_account', ''),
            order.get('login_password', ''),
            order.get('carrier', ''),
            order['quantity'],
            order['total'],
            order['payment_method'],
            '待付款',
        ])
    except Exception as e:
        print(f'儲存訂單失敗: {e}')

def update_order_status(order_id, status):
    try:
        sheet = get_sheet()
        cell = sheet.find(order_id)
        if cell:
            sheet.update_cell(cell.row, 10, status)
    except Exception as e:
        print(f'更新狀態失敗: {e}')

# ── 藍新金流 ─────────────────────────────────────────────
NEWEBPAY_URL = os.getenv('NEWEBPAY_URL', 'https://core.newebpay.com/MPG/mpg_gateway')

def gen_order_id():
    ts = datetime.now().strftime('%y%m%d%H%M%S')
    rnd = ''.join(random.choices(string.digits, k=3))
    return f'ORD{ts}{rnd}'

def newebpay_aes_encrypt(data_str, hash_key, hash_iv):
    block_size = 32
    pad_len = block_size - (len(data_str) % block_size)
    padded = data_str + chr(pad_len) * pad_len
    cipher = Cipher(
        algorithms.AES(hash_key.encode()),
        modes.CBC(hash_iv.encode()),
        backend=default_backend()
    )
    enc = cipher.encryptor()
    ct = enc.update(padded.encode()) + enc.finalize()
    return binascii.hexlify(ct).decode()

def newebpay_aes_decrypt(hex_str, hash_key, hash_iv):
    ct = binascii.unhexlify(hex_str)
    cipher = Cipher(
        algorithms.AES(hash_key.encode()),
        modes.CBC(hash_iv.encode()),
        backend=default_backend()
    )
    dec = cipher.decryptor()
    padded = dec.update(ct) + dec.finalize()
    pad_len = padded[-1]
    return padded[:-pad_len].decode()

def newebpay_sha256(trade_info, hash_key, hash_iv):
    raw = f'HashKey={hash_key}&{trade_info}&HashIV={hash_iv}'
    return hashlib.sha256(raw.encode()).hexdigest().upper()

def build_newebpay_params(order_id, amount, payment_type, base_url):
    merchant_id = os.getenv('NEWEBPAY_MERCHANT_ID')
    hash_key    = os.getenv('NEWEBPAY_HASH_KEY')
    hash_iv     = os.getenv('NEWEBPAY_HASH_IV')

    choose_payment = 'VACC' if payment_type == 'ATM' else 'CVS'

    trade_data = {
        'MerchantID':      merchant_id,
        'RespondType':     'JSON',
        'TimeStamp':       str(int(time.time())),
        'Version':         '2.0',
        'MerchantOrderNo': order_id,
        'Amt':             str(amount),
        'ItemDesc':        '遊戲點數儲值',
        'ReturnURL':       f'{base_url}/newebpay/notify',
        'ClientBackURL':   f'{base_url}/newebpay/result',
        'LoginType':       '0',
        'ChoosePayment':   choose_payment,
    }

    trade_info_str = urllib.parse.urlencode(trade_data)
    trade_info     = newebpay_aes_encrypt(trade_info_str, hash_key, hash_iv)
    trade_sha      = newebpay_sha256(trade_info, hash_key, hash_iv)

    return {
        'MerchantID': merchant_id,
        'TradeInfo':  trade_info,
        'TradeSha':   trade_sha,
        'Version':    '2.0',
    }

# ── Flask 路由 ────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    sig  = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route('/pay/<order_id>')
def pay_page(order_id):
    order = pending_orders.get(order_id)
    if not order:
        return '<h2>訂單不存在或已過期</h2>', 404
    base_url = os.getenv('BASE_URL', request.url_root.rstrip('/'))
    params   = build_newebpay_params(order_id, order['total'], order['payment_type'], base_url)
    fields   = ''.join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in params.items())
    return f'''<!DOCTYPE html><html><body>
<p>正在跳轉到付款頁面...</p>
<form id="f" action="{NEWEBPAY_URL}" method="post">{fields}</form>
<script>document.getElementById('f').submit();</script>
</body></html>'''

@app.route('/newebpay/notify', methods=['POST'])
def newebpay_notify():
    status        = request.form.get('Status', '')
    trade_info_enc = request.form.get('TradeInfo', '')
    hash_key = os.getenv('NEWEBPAY_HASH_KEY')
    hash_iv  = os.getenv('NEWEBPAY_HASH_IV')
    try:
        decrypted = newebpay_aes_decrypt(trade_info_enc, hash_key, hash_iv)
        result    = json.loads(decrypted).get('Result', {})
        order_id  = result.get('MerchantOrderNo', '')
        if status == 'SUCCESS' and order_id:
            update_order_status(order_id, '已付款')
    except Exception as e:
        print(f'藍新通知處理失敗: {e}')
    return 'OK'

@app.route('/newebpay/result')
def newebpay_result():
    return '<h2>感謝您的購買！請回到 LINE 查看訂單狀態。</h2>'

@app.route('/price')
def price_page():
    base_url  = os.getenv('BASE_URL', request.url_root.rstrip('/'))
    order_url = f'{base_url}/liff'
    return render_template('price.html', order_url=order_url)

@app.route('/liff')
def liff_page():
    liff_id = os.getenv('LIFF_ID', '')
    return render_template('liff.html', liff_id=liff_id)

@app.route('/api/send_otp', methods=['POST'])
def send_otp():
    data    = request.get_json() or {}
    user_id = data.get('userId', '').strip()
    phone   = data.get('phone', '').strip()
    import re
    if not re.match(r'^09\d{8}$', phone):
        return jsonify({'ok': False, 'msg': '請輸入正確手機號碼（09 開頭共 10 碼）'})
    if not user_id or user_id == 'unknown':
        return jsonify({'ok': False, 'msg': '請在 LINE App 內開啟此頁面'})
    otp = str(random.randint(100000, 999999))
    otp_store[user_id] = {'otp': otp, 'expires': time.time() + 300, 'phone': phone}
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(
                        text=f'【鼠來寶工作室】您的驗證碼為：{otp}\n5 分鐘內有效，請勿洩漏給他人。'
                    )]
                )
            )
        return jsonify({'ok': True})
    except Exception as e:
        print(f'OTP 發送失敗: {e}')
        return jsonify({'ok': False, 'msg': '發送失敗，請確認已加入鼠來寶工作室為好友'})

@app.route('/api/verify_otp', methods=['POST'])
def verify_otp():
    data    = request.get_json() or {}
    user_id = data.get('userId', '').strip()
    otp     = data.get('otp', '').strip()
    record  = otp_store.get(user_id)
    if not record:
        return jsonify({'valid': False, 'msg': '請先發送驗證碼'})
    if time.time() > record['expires']:
        otp_store.pop(user_id, None)
        return jsonify({'valid': False, 'msg': '驗證碼已過期，請重新發送'})
    if otp != record['otp']:
        return jsonify({'valid': False, 'msg': '驗證碼錯誤，請重新輸入'})
    otp_store.pop(user_id, None)
    return jsonify({'valid': True})

@app.route('/api/check_discount', methods=['POST'])
def check_discount():
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    try:
        codes = json.loads(os.getenv('DISCOUNT_CODES', '{}'))
    except Exception:
        codes = {}
    if code in codes:
        return jsonify({'valid': True, 'discount': int(codes[code])})
    return jsonify({'valid': False, 'msg': '無效的優惠代碼'})

@app.route('/api/verify_usdt', methods=['POST'])
def verify_usdt():
    data     = request.get_json() or {}
    code     = data.get('code', '').strip()
    expected = os.getenv('USDT_VERIFY_CODE', '').strip()
    if not expected:
        return jsonify({'valid': False, 'msg': '驗證碼未設定，請聯繫客服'})
    return jsonify({'valid': code == expected})

@app.route('/api/liff_order', methods=['POST'])
def liff_order():
    data            = request.get_json() or {}
    user_id         = data.get('userId') or 'unknown'
    vp              = data.get('vp', 0)
    base_price      = data.get('price', 0)
    riot_id         = data.get('riotId', '')
    payment_type    = data.get('paymentType', 'BANK')
    discount_amount = int(data.get('discountAmount', 0))

    cvs_fee    = 30 if payment_type == 'CVS' else 0
    final_price = base_price - discount_amount + cvs_fee

    pm_label = {'BANK': '銀行轉帳', 'CVS': '超商代碼', 'USDT': 'USDT'}.get(payment_type, payment_type)

    order_id = gen_order_id()
    order = {
        'order_id':       order_id,
        'user_id':        user_id,
        'product':        f'VALORANT {vp}VP',
        'game_account':   riot_id,
        'login_type':     data.get('loginType', ''),
        'login_account':  data.get('loginAccount', ''),
        'login_password': data.get('loginPassword', ''),
        'carrier':        data.get('carrier', ''),
        'quantity':       1,
        'total':          final_price,
        'payment_method': pm_label,
        'payment_type':   payment_type,
        'phone':          data.get('phone', ''),
        'discount_code':  data.get('discountCode', ''),
        'discount_amount': discount_amount,
    }
    pending_orders[order_id] = order
    save_order(order)

    resp = {'orderId': order_id, 'amount': final_price, 'paymentType': payment_type}

    if payment_type == 'USDT':
        rate = float(os.getenv('USDT_RATE', '32'))
        resp.update({
            'usdtNetwork': os.getenv('USDT_NETWORK', 'BEP20'),
            'usdtAddress': os.getenv('USDT_ADDRESS', ''),
            'usdtAmount':  round(final_price / rate, 2),
        })
    elif payment_type == 'CVS':
        resp['cvsNote'] = '代碼將透過 LINE 發送給您'
    else:
        resp.update({
            'bankName':    os.getenv('BANK_NAME', '將來銀行'),
            'bankCode':    os.getenv('BANK_CODE', '823'),
            'bankAccount': os.getenv('BANK_ACCOUNT', ''),
        })
    return jsonify(resp)

# ── LINE Bot 回覆 ─────────────────────────────────────
def reply(token, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=token,
                messages=[TextMessage(text=text)]
            )
        )

def start_order(uid, token):
    liff_id = os.getenv('LIFF_ID', '')
    shop_url = f'https://liff.line.me/{liff_id}' if liff_id else f'{os.getenv("BASE_URL", "http://localhost:5000")}/liff'
    reply(token,
        f'💎 鼠來寶工作室 - VALORANT 特務幣儲值\n\n'
        f'點擊下方連結開始下單：\n{shop_url}\n\n'
        f'✅ 安全來源，多年零封號記錄\n'
        f'⚡ 確認付款後 30 分鐘內到帳\n'
        f'💳 支援：銀行轉帳 / 超商代碼 / USDT'
    )

RICH_MENU_ID = 'richmenu-7d5987ffaedc4085278467cfcd6099c9'

def link_rich_menu(uid):
    try:
        import requests as req
        token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
        req.post(
            f'https://api.line.me/v2/bot/user/{uid}/richmenu/{RICH_MENU_ID}',
            headers={'Authorization': f'Bearer {token}'}
        )
    except Exception:
        pass

FAQ_SAFETY = (
    '🔒 安全性說明\n\n'
    '我們透過以下三種安全管道取得遊戲虛擬貨幣：\n'
    '• 跨區低匯率差\n'
    '• 官方點數卡經銷商\n'
    '• 遊戲官方特約串接\n\n'
    '所有來源均符合官方規範，多年來零封號記錄。'
)

FAQ_SPEED = (
    '⚡ 到帳速度說明\n\n'
    '確認付款後通常 30 分鐘內完成儲值。\n'
    '若遇人工驗證情況，最長約 2 小時。\n'
    '超商代碼需等超商系統回傳確認，時間稍長。\n\n'
    '客服時間：每日 10:00 – 22:00'
)

FAQ_PRICE = (
    '💰 關於定價\n\n'
    '價格可能隨時調整，原因包含：\n'
    '• 跨區匯率波動\n'
    '• 遊戲官方定價調整\n'
    '• 庫存成本變動\n'
    '• 平台手續費調整\n\n'
    '建議下單前確認頁面標示價格，成立後依當時價格計算。'
)

FAQ_PAYMENT = (
    '💳 付款方式\n\n'
    '🏦 銀行轉帳 — 免手續費\n'
    '   轉帳後截圖回傳確認\n\n'
    '🏪 超商代碼 — 手續費 +NT$30\n'
    '   7-11 / 全家 / 萊爾富 / OK\n\n'
    '💎 USDT — BEP20（幣安鏈）\n'
    '   需通過驗證才能使用\n\n'
    '金流合作：藍新金流 NewebPay'
)

FAQ_DISCLAIMER = (
    '📋 免責聲明\n\n'
    '• 所有商品為遊戲虛擬貨幣，購買後不支援退款。\n'
    '• 請確保提供的帳號資訊正確，輸入錯誤導致儲值失敗恕不負責。\n'
    '• 本服務遵守遊戲官方服務條款，禁止用於違規用途。\n'
    '• 疑問請透過 LINE 官方帳號聯繫客服。'
)

FAQ_BACKUP = (
    '🔑 備用碼教學\n\n'
    '📘 Facebook 復原碼\n'
    '設定 → 安全和登入 → 使用雙重驗證\n'
    '→ 復原碼 → 取得新碼\n\n'
    '🔵 Google 備用驗證碼\n'
    'myaccount.google.com → 安全性\n'
    '→ 兩步驟驗證 → 備用碼\n\n'
    '建議下單前提前準備備用碼，可加快儲值速度。'
)

FAQ_MENU = (
    '📋 鼠來寶工作室 - 常見問題\n\n'
    '請輸入以下關鍵字取得詳細說明：\n\n'
    '🔒 「安全」— 安全性說明\n'
    '⚡ 「速度」— 到帳時間\n'
    '💰 「價格」— 定價說明\n'
    '💳 「付款」— 付款方式\n'
    '📋 「免責聲明」— 服務條款\n'
    '🔑 「備用碼」— 備用碼教學\n\n'
    '輸入「下單」開始購買特務幣'
)

WELCOME_MSG = (
    '👋 歡迎來到鼠來寶工作室！\n\n'
    '💎 VALORANT 特務幣儲值服務\n\n'
    '輸入「下單」開始購買\n'
    '輸入「問題」查看常見問題\n'
    '輸入「付款」查看付款方式\n\n'
    '客服時間：每日 10:00 – 22:00'
)

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    uid   = event.source.user_id
    text  = event.message.text.strip()
    token = event.reply_token
    link_rich_menu(uid)

    if text == '取消':
        states.pop(uid, None)
        reply(token, '已取消。輸入「下單」可重新開始。')
        return

    # ── FAQ 關鍵字 ──────────────────────────────────────
    if any(kw in text for kw in ['安全', '封號', '來源', '合法']):
        reply(token, FAQ_SAFETY); return
    if any(kw in text for kw in ['速度', '多久', '幾分鐘', '到帳', '何時']):
        reply(token, FAQ_SPEED); return
    if any(kw in text for kw in ['價格', '定價', '費用', '多少錢', '匯率']):
        reply(token, FAQ_PRICE); return
    if any(kw in text for kw in ['付款', '付錢', '轉帳', '超商', 'USDT', 'usdt', '銀行']):
        reply(token, FAQ_PAYMENT); return
    if any(kw in text for kw in ['免責', '退款', '條款', '聲明']):
        reply(token, FAQ_DISCLAIMER); return
    if any(kw in text for kw in ['備用碼', '備份碼', '復原碼', 'backup', '雙重驗證']):
        reply(token, FAQ_BACKUP); return
    if any(kw in text for kw in ['問題', 'faq', 'FAQ', '常見', '說明', '幫助', 'help']):
        reply(token, FAQ_MENU); return

    step  = states.get(uid, {}).get('step', 'idle')
    state = states.get(uid, {})

    if step == 'idle':
        if any(kw in text for kw in ['下單', '購買', '買', '儲值', 'buy', 'hi', 'hello', '你好', '開始', 'order']):
            start_order(uid, token)
        else:
            reply(token, WELCOME_MSG)
        return

    if step == 'select_product':
        if text not in PRODUCTS:
            reply(token, f'❌ 請輸入 1 到 {len(PRODUCTS)} 之間的編號')
            return
        p = PRODUCTS[text]
        state['data'].update({'product': p['name'], 'price': p['price']})
        state['step'] = 'input_account'
        states[uid] = state
        reply(token, f'✅ 已選擇：{p["name"]}\n\n請輸入您的遊戲帳號/ID：')
        return

    if step == 'input_account':
        state['data']['game_account'] = text
        state['step'] = 'input_quantity'
        states[uid] = state
        reply(token, f'✅ 帳號：{text}\n\n請輸入購買數量（例如：1）：')
        return

    if step == 'input_quantity':
        if not text.isdigit() or int(text) < 1:
            reply(token, '❌ 請輸入有效的數量（正整數）')
            return
        qty   = int(text)
        total = state['data']['price'] * qty
        state['data'].update({'quantity': qty, 'total': total})
        state['step'] = 'select_payment'
        states[uid] = state
        reply(token,
            f'✅ 數量：{qty}\n'
            f'💰 合計：NT${total}\n\n'
            f'請選擇付款方式：\n'
            f'1. 超商代碼付款\n'
            f'2. ATM 轉帳\n\n'
            f'輸入 1 或 2')
        return

    if step == 'select_payment':
        if text == '1':
            pm, pt = '超商代碼', 'CVS'
        elif text == '2':
            pm, pt = 'ATM 轉帳', 'ATM'
        else:
            reply(token, '❌ 請輸入 1（超商）或 2（ATM）')
            return

        d        = state['data']
        order_id = gen_order_id()
        d.update({'order_id': order_id, 'payment_method': pm, 'payment_type': pt, 'user_id': uid})

        pending_orders[order_id] = d
        save_order(d)
        states.pop(uid, None)

        base_url = os.getenv('BASE_URL', 'http://localhost:5000')
        pay_url  = f'{base_url}/pay/{order_id}'

        reply(token,
            f'🎉 訂單建立成功！\n\n'
            f'📦 商品：{d["product"]}\n'
            f'👤 帳號：{d["game_account"]}\n'
            f'🔢 數量：{d["quantity"]}\n'
            f'💰 金額：NT${d["total"]}\n'
            f'💳 付款：{pm}\n'
            f'📋 訂單編號：{order_id}\n\n'
            f'👇 請點擊連結完成付款：\n{pay_url}\n\n'
            f'付款後系統將自動確認，點數將於 30 分鐘內發送。')
        return

if __name__ == '__main__':
    init_sheet()
    app.run(port=5000, debug=True)
