import os
import time
import random
import string
import threading
import asyncio
import requests
import discord
from discord.ext import commands
from flask import Flask, render_template, request, redirect, session, jsonify
from pymongo import MongoClient
from dotenv import load_dotenv

# 환경 변수 로드
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
app.secret_key = os.getenv("SECRET_KEY", "lakdks12@")

# Discord Bot 설정
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# 🚀 [핵심] 복구 대기열 및 상태 관리 (리스트 형태로 상세 추적)
queue_list = []
current_status = {
    "active": False, "finished": False, "target": 0,
    "success": 0, "fail": 0, "server": None, "user": None, "start_time": 0
}

# ---------------------------------------------------------
# 1. OAuth2 토큰 갱신 함수
# ---------------------------------------------------------
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

# ---------------------------------------------------------
# 2. Flask Web Dashboard & API (실시간 데이터 연동)
# ---------------------------------------------------------
@app.route('/1234', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        user_id = request.form.get('userid')
        user_pw = request.form.get('userpw')
        if user_id == "lakdks12@" and user_pw == "lakdks12@":
            session['logged_in'] = True
            return redirect('/dashboard')
        else:
            return "로그인 실패", 401
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    # 로그인 안 했어도 대기열 현황은 볼 수 있게 하려면 아래 2줄 삭제 가능
    # if not session.get('logged_in'): return redirect('/1234')
    return render_template('index.html')

# ⚡ [신규] 프론트엔드(웹)로 실시간 진행 상황을 쏴주는 API
@app.route('/api/queue', methods=['GET'])
def get_queue_status():
    global current_status, queue_list
    
    # 대기열 목록 정리
    waiting = [{"server_id": q["data"]["server_id"], "user": q["user"].name} for q in queue_list]
    
    return jsonify({
        "current": current_status,
        "waiting_queue": waiting,
        "queue_count": len(waiting)
    })

@app.route('/generate_key', methods=['POST'])
def generate_key():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    server_id = request.json.get('server_id')
    count = request.json.get('count', 0)
    key_part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    key_part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    new_key = f"Recovery-{key_part1}-{key_part2}"
    keys_collection.insert_one({"key": new_key, "server_id": server_id, "target_count": count, "used": False})
    return jsonify({"success": True, "key": new_key})

# ---------------------------------------------------------
# 3. Discord Bot UI & Logic
# ---------------------------------------------------------
class KeyInputModal(discord.ui.Modal, title='사용할 복구키 입력'):
    recovery_key = discord.ui.TextInput(
        label='복구키를 입력해주세요.', style=discord.TextStyle.short,
        placeholder='Recovery-XXXX-XXXX', required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        key_val = self.recovery_key.value
        key_data = keys_collection.find_one({"key": key_val, "used": False})
        
        if not key_data:
            await interaction.response.send_message("❌ 유효하지 않거나 이미 사용된 키입니다.", ephemeral=True)
            return
            
        keys_collection.update_one({"key": key_val}, {"$set": {"used": True}})
        view = ConfirmRecoveryView(key_data)
        await interaction.response.send_message(
            f"✅ **키 인증 성공!**\n대상 서버: `{key_data.get('server_id')}`\n목표 인원: `{key_data.get('target_count')}명`\n\n복구를 진행하시겠습니까?", 
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
        # 대기열 리스트에 추가
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

    # ⚡ [수정] 대기열 확인 시 상세 리스트가 출력되도록 변경
    @discord.ui.button(label='대기열 확인', style=discord.ButtonStyle.secondary, custom_id='check_queue')
    async def check_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        global current_status, queue_list
        embed = discord.Embed(title="≡ Recovery | 실시간 복구 대기열", color=0x32d55b)
        
        if not current_status["active"] and len(queue_list) == 0:
            embed.description = "• 현재 진행 중이거나 대기 중인 복구가 없습니다."
        else:
            desc = ""
            # 현재 진행 중인 작업 표시
            if current_status["active"]:
                total = current_status["target"]
                done = current_status["success"] + current_status["fail"]
                percent = int((done / total) * 100) if total > 0 else 0
                desc += f"**[▶ 진행 중]**\n서버 ID: `{current_status['server']}` (요청: {current_status['user']})\n진행률: **{percent}%** ({done}/{total}명)\n\n"
            
            # 대기 중인 리스트 표시
            if len(queue_list) > 0:
                desc += "**[⏳ 대기열]**\n"
                for idx, q in enumerate(queue_list):
                    desc += f"`{idx + 1}번` - 서버: {q['data']['server_id']} (요청: {q['user'].name})\n"
            
            embed.description = desc
            
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="임베드", description="복구 패널을 생성합니다.")
async def create_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="↺ Recovery | 복구키 사용하기", color=0x32d55b)
    embed.description = "• 복구키를 사용하려면 아래 버튼을 클릭해주세요."
    await interaction.response.send_message(embed=embed, view=MainPanelView())

# ---------------------------------------------------------
# 4. 백그라운드 복구 처리 Worker (순차적 처리 및 API 갱신)
# ---------------------------------------------------------
async def process_queue():
    global current_status, queue_list
    await bot.wait_until_ready()
    
    while not bot.is_closed():
        # 진행 중인 작업이 없고, 대기열에 사람이 있다면 뽑아서 실행
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
                    
                    await asyncio.sleep(1.5) # 딜레이
            else:
                for i in range(data['target_count']):
                    current_status["fail"] += 1
                    await asyncio.sleep(1.5)
            
            # 완료 처리
            current_status["active"] = False
            current_status["finished"] = True
            
            complete_embed = discord.Embed(title="Recovery | 복구 완료", color=0x32d55b)
            complete_embed.description = f"• **{guild_id}** 서버 복구가 완료되었습니다.\n• 성공: {current_status['success']}명 | 실패: {current_status['fail']}명"
            try: await user.send(embed=complete_embed)
            except: pass
            
        await asyncio.sleep(1) # 대기열 체크 주기

# ---------------------------------------------------------
# 5. 실행
# ---------------------------------------------------------
def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, use_reloader=False)

@bot.event
async def on_ready():
    await bot.tree.sync()
    bot.loop.create_task(process_queue())
    print(f"✅ Logged in as {bot.user} (Recovery Bot Ready)")

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    bot.run(TOKEN)
