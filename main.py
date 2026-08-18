import os
import time
import random
import string
import threading
import asyncio
import requests
import uuid
import discord
from discord.ext import commands
from flask import Flask, render_template, request, redirect, session, jsonify, url_for
from pymongo import MongoClient
from dotenv import load_dotenv

# ==========================================
# 1. 환경 변수 및 기본 설정
# ==========================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

# MongoDB 설정
client = MongoClient(MONGO_URI)
db = client['recovery_db']
keys_collection = db['recovery_keys']
oauth_collection = db['oauth_users']

# Flask 설정
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24))

# Discord Bot 설정
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# 복구 대기열 및 상태 관리
queue_list = []
current_status = {
    "active": False, "finished": False, "target": 0,
    "success": 0, "fail": 0, "server": None, "user": None, "start_time": 0
}

# ==========================================
# 2. OAuth2 토큰 갱신 함수
# ==========================================
def refresh_access_token(refresh_token):
    url = "https://discord.com/api/v10/oauth2/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    response = requests.post(url, data=data)
    if response.status_code == 200:
        return response.json().get("access_token")
    return None

# ==========================================
# 3. Flask Web Dashboard & API
# ==========================================
@app.route('/')
def index():
    if session.get('logged_in'):
        return redirect(url_for('admin_portal'))
    return render_template('index.html', is_admin=False, show_login=False)

@app.route('/1234', methods=['GET', 'POST'])
def admin_portal():
    error_msg = None
    if request.method == 'POST':
        user_id = request.form.get('userid')
        user_pw = request.form.get('userpw')
        
        if user_id == "lakdks12@" and user_pw == "lakdks12@":
            session['logged_in'] = True
            return redirect(url_for('admin_portal'))
        else:
            error_msg = "아이디 또는 비밀번호가 일치하지 않습니다."

    if session.get('logged_in'):
        return render_template('index.html', is_admin=True, show_login=False)
    
    return render_template('index.html', is_admin=False, show_login=True, error_msg=error_msg)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))

@app.route('/api/queue', methods=['GET'])
def get_queue_status():
    global current_status, queue_list
    waiting = [{"server_id": q["data"].get("server_id", "직접 입력"), "user": q["user"].name} for q in queue_list]
    return jsonify({
        "current": current_status,
        "waiting_queue": waiting,
        "queue_count": len(waiting)
    })

@app.route('/generate_key', methods=['POST'])
def generate_key():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    count = data.get('count', 0)
    
    if not count:
        return jsonify({"error": "복구 인원수를 정확히 입력해주세요."}), 400

    # RCV-XXXX-XXXX 포맷 키 생성 (서버 ID는 키 발급 시점에 고정하지 않음)
    raw_uuid = str(uuid.uuid4()).upper().split('-')
    new_key = f"RCV-{raw_uuid[0][:4]}-{raw_uuid[1][:4]}"
    
    # MongoDB에 저장 (target_count만 저장)
    keys_collection.insert_one({
        "key": new_key, 
        "target_count": int(count), 
        "used": False
    })
    
    return jsonify({"success": True, "key": new_key})

# ==========================================
# 4. Discord Bot UI & Logic
# ==========================================
class KeyInputModal(discord.ui.Modal, title='복구 정보 입력'):
    recovery_key = discord.ui.TextInput(
        label='복구키를 입력해주세요.', style=discord.TextStyle.short,
        placeholder='RCV-XXXX-XXXX', required=True
    )
    target_server_id = discord.ui.TextInput(
        label='복구할 대상 서버 ID를 입력하세요.', style=discord.TextStyle.short,
        placeholder='123456789012345678', required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        key_val = self.recovery_key.value
        server_id_val = self.target_server_id.value.strip()
        
        key_data = keys_collection.find_one({"key": key_val, "used": False})
        
        if not key_data:
            await interaction.response.send_message("❌ 유효하지 않거나 이미 사용된 키입니다.", ephemeral=True)
            return
            
        # 키 데이터에 유저가 입력한 서버 ID 탑재
        key_data["server_id"] = server_id_val
        keys_collection.update_one({"key": key_val}, {"$set": {"used": True, "server_id": server_id_val}})
        
        view = ConfirmRecoveryView(key_data)
        await interaction.response.send_message(
            f"✅ **키 인증 성공!**\n대상 서버: `{server_id_val}`\n목표 인원: `{key_data.get('target_count')}명`\n\n복구를 진행하시겠습니까?", 
            view=view, ephemeral=True
        )

class ConfirmRecoveryView(discord.ui.View):
    def __init__(self, key_data):
        super().__init__(timeout=60)
        self.key_data = key_data
        self.add_item(discord.ui.Button(label='봇 초대하기', style=discord.ButtonStyle.link, url=f'https://discord.com/oauth2/authorize?client_id={CLIENT_ID}&permissions=8&scope=bot'))

    @discord.ui.button(label='복구 시작하기', style=discord.ButtonStyle.green)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        global queue_list, current_status
        
        queue_list.append({
            "user": interaction.user,
            "data": self.key_data
        })
        queue_position = len(queue_list)
        
        embed = discord.Embed(title="≡ Recovery | 대기열 등록", color=0x32d55b)
        if current_status["active"]:
            embed.description = f"• 현재 다른 서버의 복구가 진행 중입니다.\n• 귀하의 작업은 대기열 **{queue_position}번**으로 등록되었습니다.\n• 순서가 되면 자동으로 복구가 시작됩니다."
        else:
            embed.description = "• 복구 대기열에 등록되었으며, 즉시 동기화가 시작됩니다.\n• 웹 대시보드에서 실시간 진행 상황을 확인하세요."
            
        await interaction.response.send_message(embed=embed, ephemeral=True)

class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='복구봇 사용하기', style=discord.ButtonStyle.secondary, custom_id='use_bot')
    async def use_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyInputModal())

    @discord.ui.button(label='대기열 확인', style=discord.ButtonStyle.secondary, custom_id='check_queue')
    async def check_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        global current_status, queue_list
        embed = discord.Embed(title="≡ Recovery | 실시간 복구 대기열", color=0x32d55b)
        
        if not current_status["active"] and len(queue_list) == 0:
            embed.description = "• 현재 진행 중이거나 대기 중인 복구가 없습니다."
        else:
            desc = ""
            if current_status["active"]:
                total = current_status["target"]
                done = current_status["success"] + current_status["fail"]
                percent = int((done / total) * 100) if total > 0 else 0
                desc += f"**[▶ 진행 중]**\n서버 ID: `{current_status['server']}` (요청: {current_status['user']})\n진행률: **{percent}%** ({done}/{total}명)\n\n"
            
            if len(queue_list) > 0:
                desc += "**[⏳ 대기열]**\n"
                for idx, q in enumerate(queue_list):
                    desc += f"`{idx + 1}번` - 서버: {q['data'].get('server_id', '미정')} (요청: {q['user'].name})\n"
            
            embed.description = desc
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="임베드", description="복구 패널을 생성합니다.")
async def create_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="↺ Recovery | 복구키 사용하기", color=0x32d55b)
    embed.description = "• 복구키를 사용하려면 아래 버튼을 클릭해주세요."
    await interaction.response.send_message(embed=embed, view=MainPanelView())

# ==========================================
# 5. 백그라운드 복구 처리 Worker
# ==========================================
async def process_queue():
    global current_status, queue_list
    await bot.wait_until_ready()
    
    while not bot.is_closed():
        if len(queue_list) > 0 and not current_status["active"]:
            task = queue_list.pop(0)
            user = task["user"]
            data = task["data"]
            guild_id = data["server_id"]
            
            target_users = list(oauth_collection.find({"guild_id": guild_id}))
            actual_target_count = len(target_users)
            final_target = actual_target_count if actual_target_count > 0 else data['target_count']
            
            current_status.update({
                "active": True, "finished": False, "target": final_target,
                "success": 0, "fail": 0, "server": guild_id, "user": user.name,
                "start_time": time.time()
            })
            
            dm_embed = discord.Embed(title="Recovery | 복구 진행 시작", description=f"• **{guild_id}** 서버의 복구가 시작되었습니다.\n• 실시간 대시보드에서 게이지를 확인하세요.", color=0x32d55b)
            try: await user.send(embed=dm_embed)
            except: pass
                
            headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
            
            if target_users:
                for user_data in target_users:
                    user_id = user_data.get("user_id")
                    refresh_token = user_data.get("oauth_refresh_token")
                    access_token = user_data.get("access_token")
                    
                    if not access_token and refresh_token:
                        access_token = refresh_access_token(refresh_token)
                    
                    if not user_id or not access_token:
                        current_status["fail"] += 1
                        continue
                    
                    url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
                    try:
                        response = requests.put(url, headers=headers, json={"access_token": access_token})
                        if response.status_code in [201, 204]:
                            current_status["success"] += 1
                        else:
                            current_status["fail"] += 1
                    except:
                        current_status["fail"] += 1
                    
                    await asyncio.sleep(1.5)
            else:
                for i in range(data['target_count']):
                    current_status["fail"] += 1
                    await asyncio.sleep(1.5)
            
            current_status["active"] = False
            current_status["finished"] = True
            
            complete_embed = discord.Embed(title="Recovery | 복구 완료", color=0x32d55b)
            complete_embed.description = f"• **{guild_id}** 서버 복구가 완료되었습니다.\n• 성공: {current_status['success']}명 | 실패: {current_status['fail']}명"
            try: await user.send(embed=complete_embed)
            except: pass
            
        await asyncio.sleep(1)

# ==========================================
# 6. 실행
# ==========================================
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

@bot.event
async def on_ready():
    bot.add_view(MainPanelView())
    await bot.tree.sync()
    bot.loop.create_task(process_queue())
    print(f"✅ Logged in as {bot.user} (Recovery Bot Ready)")

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    bot.run(TOKEN)
