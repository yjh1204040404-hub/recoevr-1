import os
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

# MongoDB 설정 (제공된 URI 반영)
client = MongoClient(MONGO_URI)
db = client['recovery_db'] # DB 이름
keys_collection = db['recovery_keys']
oauth_collection = db['oauth_users'] # 실제 유저 토큰 컬렉션

# Flask 설정
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_vaxis_key")

# Discord Bot 설정
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)

# 복구 대기열 및 상태 관리
recovery_queue = asyncio.Queue()
current_recovery = None
current_status = {
    "active": False, "finished": False, "target": 0,
    "success": 0, "fail": 0, "server": None, "user": None
}

# ---------------------------------------------------------
# 1. OAuth2 토큰 갱신 함수 (Refresh Token -> Access Token)
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
# 2. Flask Web Dashboard (키 생성 및 로그인 패널)
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
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect('/1234')
    return render_template('index.html')

@app.route('/generate_key', methods=['POST'])
def generate_key():
    if not session.get('logged_in'):
        return jsonify({"error": "Unauthorized"}), 401
    
    server_id = request.json.get('server_id')
    count = request.json.get('count', 0)
    
    key_part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    key_part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    new_key = f"Recovery-{key_part1}-{key_part2}"
    
    keys_collection.insert_one({
        "key": new_key,
        "server_id": server_id,
        "target_count": count,
        "used": False
    })
    
    return jsonify({"success": True, "key": new_key})

# ---------------------------------------------------------
# 3. Discord Bot UI & Logic
# ---------------------------------------------------------

class KeyInputModal(discord.ui.Modal, title='사용할 복구키 입력'):
    recovery_key = discord.ui.TextInput(
        label='복구키를 입력해주세요. *',
        style=discord.TextStyle.short,
        placeholder='Recovery-XXXX-XXXX',
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        key_val = self.recovery_key.value
        key_data = keys_collection.find_one({"key": key_val, "used": False})
        
        if not key_data:
            await interaction.response.send_message("유효하지 않거나 이미 사용된 키입니다.", ephemeral=True)
            return
        
        keys_collection.update_one({"key": key_val}, {"$set": {"used": True}})
        
        view = ConfirmRecoveryView(key_data)
        await interaction.response.send_message(
            f"유효한 키입니다.\n대상 서버: {key_data.get('server_id')}\n목표 인원: {key_data.get('target_count')}명\n복구를 진행하시겠습니까?", 
            view=view, 
            ephemeral=True
        )

class ConfirmRecoveryView(discord.ui.View):
    def __init__(self, key_data):
        super().__init__(timeout=60)
        self.key_data = key_data
        # 환경변수 기반 초대 링크 동적 설정
        self.add_item(discord.ui.Button(label='봇 초대하기', style=discord.ButtonStyle.link, url=f'https://discord.com/oauth2/authorize?client_id={CLIENT_ID}&permissions=8&scope=bot'))

    @discord.ui.button(label='복구 시작하기', style=discord.ButtonStyle.green)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue_position = recovery_queue.qsize() + 1
        await recovery_queue.put({
            "user": interaction.user,
            "data": self.key_data
        })
        
        if current_recovery:
            embed = discord.Embed(title="≡ Recovery | 대기열 등록", color=0x32d55b)
            embed.description = f"• 현재 다른 복구가 진행 중입니다.\n• 대기열 **{queue_position}번**으로 등록되었습니다.\n• 순서가 되면 자동으로 복구가 시작됩니다."
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("복구 대기열에 등록되었으며, 즉시 시작됩니다.", ephemeral=True)

class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='복구봇 사용하기', style=discord.ButtonStyle.secondary, custom_id='use_bot')
    async def use_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KeyInputModal())

    @discord.ui.button(label='대기열 확인', style=discord.ButtonStyle.secondary, custom_id='check_queue')
    async def check_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="≡ Recovery | 복구 대기열", color=0x32d55b)
        if recovery_queue.empty() and not current_recovery:
            embed.description = "• 현재 진행 중이거나 대기 중인 복구가 없습니다."
        else:
            embed.description = f"• 현재 진행 중: 1개\n• 대기 중: {recovery_queue.qsize()}개"
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="임베드", description="복구 패널을 생성합니다.")
async def create_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="↺ Recovery | 복구키 사용하기", color=0x32d55b)
    embed.description = "• 복구키를 사용하려면 아래 버튼을 클릭해주세요."
    await interaction.response.send_message(embed=embed, view=MainPanelView())

# ---------------------------------------------------------
# 4. 백그라운드 복구 처리 (실제 DB 토큰 연동 및 API 가입 로직)
# ---------------------------------------------------------
async def process_queue():
    global current_recovery, current_status
    await bot.wait_until_ready()
    
    while not bot.is_closed():
        task = await recovery_queue.get()
        current_recovery = task
        
        user = task["user"]
        data = task["data"]
        guild_id = data["server_id"]
        
        # 몽고DB에서 해당 서버의 유저 토큰 데이터 가져오기 (스크린샷 구조 반영)
        target_users = list(oauth_collection.find({"guild_id": guild_id}))
        actual_target_count = len(target_users)
        
        current_status.update({
            "active": True,
            "finished": False,
            "target": actual_target_count if actual_target_count > 0 else data['target_count'],
            "success": 0,
            "fail": 0,
            "server": guild_id,
            "user": user.name
        })
        
        # DM 전송 알림
        dm_embed = discord.Embed(title="Recovery | 복구 진행 시작", description=f"• **{guild_id}** 서버의 복구가 시작되었습니다.", color=0x32d55b)
        try:
            await user.send(embed=dm_embed)
        except:
            pass
            
        headers = {
            "Authorization": f"Bot {TOKEN}",
            "Content-Type": "application/json"
        }
        
        # 실제 DB 유저가 존재하면 순차적으로 API 요청 전송
        if target_users:
            for user_data in target_users:
                user_id = user_data.get("user_id")
                refresh_token = user_data.get("oauth_refresh_token")
                
                # 엑세스 토큰이 없거나 갱신이 필요할 경우 리프레시 토큰으로 발급
                access_token = user_data.get("access_token")
                if not access_token and refresh_token:
                    access_token = refresh_access_token(refresh_token)
                
                if not user_id or not access_token:
                    current_status["fail"] += 1
                    continue
                
                url = f"https://discord.com/api/v10/guilds/{guild_id}/members/{user_id}"
                payload = {"access_token": access_token}
                
                try:
                    response = requests.put(url, headers=headers, json=payload)
                    if response.status_code in [201, 204]:
                        current_status["success"] += 1
                    else:
                        current_status["fail"] += 1
                except Exception:
                    current_status["fail"] += 1
                    
                # API 제재(Rate Limit) 방지용 딜레이
                await asyncio.sleep(1.5)
        else:
            # DB에 일치하는 데이터가 없을 경우 키에 설정된 카운트만큼 더미/대체 처리
            for i in range(data['target_count']):
                current_status["fail"] += 1
                await asyncio.sleep(1.5)
        
        current_status["finished"] = True
        
        # 완료 DM 전송
        complete_embed = discord.Embed(title="Recovery | 복구 완료", color=0x32d55b)
        complete_embed.description = f"• **{guild_id}** 서버 복구가 완료되었습니다.\n• 성공: {current_status['success']}명 | 실패: {current_status['fail']}명"
        try:
            await user.send(embed=complete_embed)
        except:
            pass
            
        current_recovery = None
        current_status["active"] = False
        recovery_queue.task_done()

# ---------------------------------------------------------
# 5. 실행 (Flask + Discord Bot 통합)
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
    t = threading.Thread(target=run_flask)
    t.start()
    bot.run(TOKEN)
