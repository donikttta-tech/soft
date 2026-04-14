import asyncio
import os
import cv2
import numpy as np
import tempfile
import logging
import json
import random
from datetime import datetime, date, timedelta
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, FSInputFile, LabeledPrice,
    PreCheckoutQuery
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from ultralytics import YOLO

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN        = "8636167645:AAGigEfCp9Hoqxu5mQsNR3R60olRxXSp0RE"
ADMIN_ID     = 8144110555
BOT_USERNAME = "freesoftik_bot"

bot   = Bot(token=TOKEN)
dp    = Dispatcher(storage=MemoryStorage())
model = YOLO("yolov8n-pose.pt")

# ═══════════════════════════════════════════════════════════════
# БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════
DB_FILE = "users_db.json"

def load_db() -> dict:
    if not Path(DB_FILE).exists():
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def get_user(user_id: int) -> dict:
    db  = load_db()
    uid = str(user_id)
    if uid not in db:
        db[uid] = {
            "user_id":         user_id,
            "username":        "",
            "videos_today":    0,
            "last_video_date": "",
            "vip_until":       "",
            "extra_videos":    0,
            "referred_by":     None,
            "referrals":       [],
            "total_videos":    0,
            "verified":        False,   # прошёл ли проверку
        }
        save_db(db)
    return db[uid]

def save_user(u: dict):
    db = load_db()
    db[str(u["user_id"])] = u
    save_db(db)

def is_vip(u: dict) -> bool:
    if not u.get("vip_until"):
        return False
    try:
        return datetime.strptime(u["vip_until"], "%Y-%m-%d").date() >= date.today()
    except Exception:
        return False

def get_daily_limit(u: dict) -> int:
    return (10 if is_vip(u) else 1) + u.get("extra_videos", 0)

def get_videos_left(u: dict) -> int:
    if u.get("last_video_date") != date.today().isoformat():
        return get_daily_limit(u)
    return max(0, get_daily_limit(u) - u.get("videos_today", 0))

def use_video(u: dict) -> bool:
    today = date.today().isoformat()
    if u.get("last_video_date") != today:
        u["videos_today"]    = 0
        u["last_video_date"] = today
    if u["videos_today"] >= get_daily_limit(u):
        return False
    u["videos_today"]  += 1
    u["total_videos"]   = u.get("total_videos", 0) + 1
    save_user(u)
    return True


# ═══════════════════════════════════════════════════════════════
# КАПЧА — генерация примера
# ═══════════════════════════════════════════════════════════════

def generate_captcha() -> tuple[str, int]:
    """
    Возвращает (текст вопроса, правильный ответ).
    Используем простые математические примеры.
    """
    ops = ["+", "-", "×"]
    op  = random.choice(ops)

    if op == "+":
        a, b   = random.randint(1, 20), random.randint(1, 20)
        answer = a + b
        text   = f"{a} + {b}"
    elif op == "-":
        a      = random.randint(5, 25)
        b      = random.randint(1, a)
        answer = a - b
        text   = f"{a} - {b}"
    else:  # ×
        a, b   = random.randint(2, 9), random.randint(2, 9)
        answer = a * b
        text   = f"{a} × {b}"

    return text, answer

def generate_captcha_keyboard(correct: int) -> InlineKeyboardMarkup:
    """
    4 варианта ответа — один правильный, три случайных.
    Перемешиваем.
    """
    wrong_answers = set()
    while len(wrong_answers) < 3:
        delta  = random.randint(-5, 5)
        wrong  = correct + delta
        if wrong != correct and wrong > 0:
            wrong_answers.add(wrong)

    options = list(wrong_answers) + [correct]
    random.shuffle(options)

    buttons = [
        InlineKeyboardButton(
            text=str(opt),
            callback_data=f"captcha_{'ok' if opt == correct else 'fail'}_{opt}"
        )
        for opt in options
    ]

    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[:2],
        buttons[2:],
    ])


# ═══════════════════════════════════════════════════════════════
# FSM
# ═══════════════════════════════════════════════════════════════
class S(StatesGroup):
    captcha  = State()   # ожидание решения капчи
    main     = State()
    mode     = State()
    video    = State()


# ═══════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Начать",              callback_data="go_mode",  style="primary")],
        [InlineKeyboardButton(text="👑 VIP подписка",        callback_data="go_vip",   style="success")],
        [InlineKeyboardButton(text="👥 Реферальная система", callback_data="go_ref",   style="primary")],
    ])

def kb_mode():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 3D Box", callback_data="esp_3d",    style="primary"),
            InlineKeyboardButton(text="⬜ 2D Box", callback_data="esp_2d",    style="primary"),
        ],
        [InlineKeyboardButton(text="🦴 Скелет",   callback_data="esp_bones", style="success")],
        [InlineKeyboardButton(text="◀️ Меню",     callback_data="go_main",   style="danger")],
    ])

def kb_vip():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить VIP — 25 звёзд/мес", callback_data="buy_vip", style="success")],
        [InlineKeyboardButton(text="◀️ Назад",                      callback_data="go_main", style="danger")],
    ])

def kb_ref(uid: int):
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔗 Поделиться ссылкой",
            url=(
                f"https://t.me/share/url"
                f"?url={ref_link}"
                f"&text=Попробуй%20ESP%20Vision%20Bot!"
            ),
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="go_main", style="danger")],
    ])

def kb_after():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Ещё раз", callback_data="go_mode",  style="primary"),
            InlineKeyboardButton(text="🏠 Меню",     callback_data="go_main",  style="danger"),
        ],
    ])

def kb_limit():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 Купить VIP",       callback_data="go_vip", style="success")],
        [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="go_ref", style="primary")],
        [InlineKeyboardButton(text="◀️ Меню",             callback_data="go_main",style="danger")],
    ])


# ═══════════════════════════════════════════════════════════════
# CS2-STYLE ESP (без изменений — вся логика рисования)
# ═══════════════════════════════════════════════════════════════
BONES = [
    (0,1),(0,2),(1,3),(2,4),
    (5,6),(5,7),(7,9),(6,8),(8,10),
    (5,11),(6,12),(11,12),
    (11,13),(13,15),(12,14),(14,16),
]
BONE_COLOR = {
    "head": (0,200,255), "body": (0,255,100),
    "larm": (255,150,0), "rarm": (255,150,0),
    "lleg": (100,100,255), "rleg": (100,100,255),
}
BONE_GROUP_MAP = {
    (0,1):"head",(0,2):"head",(1,3):"head",(2,4):"head",
    (5,6):"body",(5,11):"body",(6,12):"body",(11,12):"body",
    (5,7):"larm",(7,9):"larm",(6,8):"rarm",(8,10):"rarm",
    (11,13):"lleg",(13,15):"lleg",(12,14):"rleg",(14,16):"rleg",
}
PALETTE = [
    (0,255,0),(255,50,50),(50,150,255),(255,200,0),
    (0,255,255),(255,0,200),(255,120,0),(150,255,50),
]

def person_color(idx): return PALETTE[idx % len(PALETTE)]

def draw_filled_rect(img,x1,y1,x2,y2,color,alpha=0.15):
    roi=img[y1:y2,x1:x2]
    if roi.size==0: return
    cv2.addWeighted(np.full_like(roi,color),alpha,roi,1-alpha,0,roi)
    img[y1:y2,x1:x2]=roi

def draw_corner_rect(img,x1,y1,x2,y2,color,t=2,seg=0.22):
    lx=max(8,int((x2-x1)*seg)); ly=max(8,int((y2-y1)*seg))
    for o,h,v in [
        ((x1,y1),(x1+lx,y1),(x1,y1+ly)),
        ((x2,y1),(x2-lx,y1),(x2,y1+ly)),
        ((x1,y2),(x1+lx,y2),(x1,y2-ly)),
        ((x2,y2),(x2-lx,y2),(x2,y2-ly)),
    ]:
        cv2.line(img,o,h,(0,0,0),t+2,cv2.LINE_AA)
        cv2.line(img,o,v,(0,0,0),t+2,cv2.LINE_AA)
        cv2.line(img,o,h,color,t,cv2.LINE_AA)
        cv2.line(img,o,v,color,t,cv2.LINE_AA)

def draw_3d_box(img,x1,y1,x2,y2,color,t=2):
    w=x2-x1; h=y2-y1
    dx=int(w*0.18); dy=int(h*0.12)
    front=np.array([[x1,y2],[x2,y2],[x2,y1],[x1,y1]],np.int32)
    bx1b,by1b,bx2b,by2b=x1+dx,y1-dy,x2+dx,y2-dy
    back=np.array([[bx1b,by2b],[bx2b,by2b],[bx2b,by1b],[bx1b,by1b]],np.int32)
    tmp=img.copy()
    cv2.fillPoly(tmp,[back],tuple(c//4 for c in color))
    cv2.addWeighted(tmp,0.3,img,0.7,0,img)
    for p1,p2 in [(front[i],back[i]) for i in range(4)]:
        cv2.line(img,tuple(p1),tuple(p2),(0,0,0),t+2,cv2.LINE_AA)
        cv2.line(img,tuple(p1),tuple(p2),color,t,cv2.LINE_AA)
    for pts,c in [(back,tuple(x//2 for x in color)),(front,color)]:
        cv2.polylines(img,[pts],True,(0,0,0),t+2,cv2.LINE_AA)
        cv2.polylines(img,[pts],True,c,t,cv2.LINE_AA)

def draw_label_cs2(img,x1,y1,color,text,sub=""):
    font=cv2.FONT_HERSHEY_SIMPLEX; scale=0.45; thick=1; pad=4
    lines=[text]+([sub] if sub else [])
    sizes=[cv2.getTextSize(l,font,scale,thick)[0] for l in lines]
    max_w=max(s[0] for s in sizes)
    total_h=sum(s[1] for s in sizes)+pad*(len(lines)+1)
    bx1,by1b,bx2,by2=x1,y1-total_h-4,x1+max_w+pad*2,y1-2
    cv2.rectangle(img,(bx1,by1b),(bx2,by2),(15,15,15),-1)
    cv2.rectangle(img,(bx1,by1b),(bx2,by1b+2),color,-1)
    cv2.rectangle(img,(bx1,by1b),(bx2,by2),color,1)
    cy=by1b+pad+sizes[0][1]
    for line,(_,th) in zip(lines,sizes):
        cv2.putText(img,line,(bx1+pad,cy),font,scale,(255,255,255),thick,cv2.LINE_AA)
        cy+=th+pad

def draw_healthbar_cs2(img,x1,y1,x2,y2,conf):
    bw=5; bx=x1-bw-3; bh=y2-y1
    if bh<=0: return
    cv2.rectangle(img,(bx,y1),(bx+bw,y2),(20,20,20),-1)
    cv2.rectangle(img,(bx,y1),(bx+bw,y2),(80,80,80),1)
    fill=int(bh*conf); fy1=y2-fill
    c=(0,230,0) if conf>0.75 else (0,200,200) if conf>0.55 else (0,80,255)
    cv2.rectangle(img,(bx,fy1),(bx+bw,y2),c,-1)
    cv2.putText(img,f"{int(conf*100)}",(bx-2,y1-4),
                cv2.FONT_HERSHEY_SIMPLEX,0.32,(200,200,200),1,cv2.LINE_AA)

def draw_snap_line(img,x1,y1,x2,y2,color):
    H,W=img.shape[:2]; cx=(x1+x2)//2
    cv2.line(img,(W//2,H),(cx,y2),(0,0,0),2,cv2.LINE_AA)
    cv2.line(img,(W//2,H),(cx,y2),color,1,cv2.LINE_AA)

def draw_skeleton_cs2(img,kps,color):
    kps=kps.astype(np.int32)
    for a,b in BONES:
        if a>=len(kps) or b>=len(kps): continue
        ax,ay=kps[a]; bx,by=kps[b]
        if ax<2 and ay<2: continue
        if bx<2 and by<2: continue
        key=(min(a,b),max(a,b))
        bc=BONE_COLOR[BONE_GROUP_MAP.get(key,"body")]
        cv2.line(img,(ax,ay),(bx,by),(0,0,0),5,cv2.LINE_AA)
        cv2.line(img,(ax,ay),(bx,by),bc,2,cv2.LINE_AA)
    for i,(kx,ky) in enumerate(kps):
        if kx<2 and ky<2: continue
        r=5 if i==0 else 3
        cv2.circle(img,(kx,ky),r+2,(0,0,0),-1,cv2.LINE_AA)
        cv2.circle(img,(kx,ky),r,color,-1,cv2.LINE_AA)
        cv2.circle(img,(kx,ky),r-1,(255,255,255),1,cv2.LINE_AA)

def draw_head_circle(img,kps,color):
    pts=[(int(kps[i][0]),int(kps[i][1])) for i in range(min(5,len(kps)))
         if kps[i][0]>2 or kps[i][1]>2]
    if not pts: return
    cx=sum(p[0] for p in pts)//len(pts)
    cy=sum(p[1] for p in pts)//len(pts)
    r=max(12,int(max(abs(p[0]-cx) for p in pts)*1.4))
    cv2.circle(img,(cx,cy),r+2,(0,0,0),2,cv2.LINE_AA)
    cv2.circle(img,(cx,cy),r,color,2,cv2.LINE_AA)

def process_frame(frame: np.ndarray, esp_mode: str) -> np.ndarray:
    H,W=frame.shape[:2]
    results=model(frame,classes=[0],verbose=False,conf=0.38,iou=0.45)
    layer=frame.copy()
    for result in results:
        boxes=result.boxes
        if boxes is None or len(boxes)==0: continue
        has_kp=(hasattr(result,"keypoints") and result.keypoints is not None
                and result.keypoints.xy is not None and len(result.keypoints.xy)>0)
        for idx in range(len(boxes)):
            x1,y1,x2,y2=map(int,boxes[idx].xyxy[0])
            conf=float(boxes[idx].conf[0])
            x1=max(0,x1); y1=max(0,y1); x2=min(W-1,x2); y2=min(H-1,y2)
            if x2-x1<10 or y2-y1<10: continue
            col=person_color(idx)
            draw_healthbar_cs2(layer,x1,y1,x2,y2,conf)
            draw_snap_line(layer,x1,y1,x2,y2,col)
            if esp_mode=="esp_2d":
                draw_filled_rect(layer,x1,y1,x2,y2,col,alpha=0.08)
                draw_corner_rect(layer,x1,y1,x2,y2,col,t=2)
                draw_label_cs2(layer,x1,y1,col,f"PLAYER {idx+1}",f"Conf: {int(conf*100)}%")
            elif esp_mode=="esp_3d":
                draw_filled_rect(layer,x1,y1,x2,y2,col,alpha=0.06)
                draw_3d_box(layer,x1,y1,x2,y2,col,t=2)
                draw_label_cs2(layer,x1,y1,col,f"PLAYER {idx+1}",f"Conf: {int(conf*100)}%")
            elif esp_mode=="esp_bones":
                draw_corner_rect(layer,x1,y1,x2,y2,tuple(c//3 for c in col),t=1)
                draw_label_cs2(layer,x1,y1,col,f"PLAYER {idx+1}",f"Conf: {int(conf*100)}%")
                if has_kp and idx<len(result.keypoints.xy):
                    kps=result.keypoints.xy[idx].cpu().numpy()
                    if kps.shape[0]==17:
                        draw_skeleton_cs2(layer,kps,col)
                        draw_head_circle(layer,kps,col)
    cv2.addWeighted(layer,0.93,frame,0.07,0,frame)
    cv2.putText(frame,"ESP Vision",(W-110,H-8),
                cv2.FONT_HERSHEY_SIMPLEX,0.38,(0,180,0),1,cv2.LINE_AA)
    return frame


# ═══════════════════════════════════════════════════════════════
# ОБРАБОТКА ВИДЕО
# ═══════════════════════════════════════════════════════════════
async def process_video(inp,out_path,esp_mode,prog):
    cap=cv2.VideoCapture(inp)
    if not cap.isOpened(): raise RuntimeError("Не удалось открыть видео")
    fps=cap.get(cv2.CAP_PROP_FPS) or 30.0
    W=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fourcc=cv2.VideoWriter_fourcc(*"avc1")
    writer=cv2.VideoWriter(out_path,fourcc,fps,(W,H))
    if not writer.isOpened():
        writer=cv2.VideoWriter(out_path,cv2.VideoWriter_fourcc(*"mp4v"),fps,(W,H))
    n=0; last_pct=-1
    while True:
        ret,frame=cap.read()
        if not ret: break
        writer.write(process_frame(frame,esp_mode))
        n+=1
        if total>0:
            pct=int(n/total*100)
            if pct!=last_pct and pct%5==0:
                last_pct=pct
                bar="█"*(pct//10)+"░"*(10-pct//10)
                try:
                    await prog.edit_text(
                        f"⚙️ <b>Обработка...</b>\n\n"
                        f"<code>[{bar}]</code> {pct}%\n"
                        f"🎬 {n}/{total} кадров",parse_mode="HTML")
                except Exception: pass
        if n%15==0: await asyncio.sleep(0.001)
    cap.release(); writer.release()


# ═══════════════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════════════

# ── /start ────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid   = message.from_user.id
    uname = message.from_user.username or ""
    args  = message.text.split(maxsplit=1)

    user = get_user(uid)
    user["username"] = uname

    # Если пришёл по реферальной ссылке и ещё не верифицирован
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1][4:])
        except ValueError:
            ref_id = None

        # Сохраняем referrer во FSM чтобы начислить бонус ПОСЛЕ капчи
        if ref_id and ref_id != uid and user.get("referred_by") is None:
            await state.update_data(pending_ref=ref_id)

            # Показываем капчу
            question, answer = generate_captcha()
            await state.update_data(captcha_answer=answer, captcha_attempts=0)
            await state.set_state(S.captcha)

            await message.answer(
                "🤖 <b>Проверка: ты не бот?</b>\n\n"
                f"Реши пример: <b>{question} = ?</b>\n\n"
                "<i>Выбери правильный ответ:</i>",
                parse_mode="HTML",
                reply_markup=generate_captcha_keyboard(answer)
            )
            save_user(user)
            return

    # Обычный /start без реферала или уже верифицирован
    save_user(user)
    await show_main(message, state, user)


# ── Капча — нажатие кнопки ────────────────────────────────────
@dp.callback_query(F.data.startswith("captcha_"), S.captcha)
async def captcha_answer(cb: CallbackQuery, state: FSMContext):
    parts  = cb.data.split("_")   # captcha_ok_5  или  captcha_fail_3
    result = parts[1]             # "ok" или "fail"

    data = await state.get_data()
    attempts = data.get("captcha_attempts", 0)

    if result == "ok":
        # ✅ Верификация пройдена
        uid  = cb.from_user.id
        user = get_user(uid)
        user["verified"] = True

        ref_id = data.get("pending_ref")
        bonus_text = ""

        if ref_id:
            ref_user = get_user(ref_id)

            # Бонус рефереру
            ref_user["extra_videos"] = ref_user.get("extra_videos", 0) + 1
            if uid not in ref_user["referrals"]:
                ref_user["referrals"].append(uid)
            save_user(ref_user)

            # Бонус новому юзеру
            user["extra_videos"]  = user.get("extra_videos", 0) + 1
            user["referred_by"]   = ref_id
            bonus_text = "\n🎁 <b>+1 бонусное видео за реферал!</b>"

            # Уведомляем реферера
            try:
                ref_uname = cb.from_user.username or str(uid)
                await bot.send_message(
                    ref_id,
                    f"🎉 <b>По вашей ссылке пришёл новый пользователь!</b>\n"
                    f"👤 @{ref_uname}\n"
                    f"✅ Проверку на бота прошёл\n"
                    f"🎬 Вам начислено <b>+1 видео</b>!",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        save_user(user)
        await state.clear()

        await cb.message.edit_text(
            f"✅ <b>Проверка пройдена! Ты не бот 😄</b>"
            f"{bonus_text}\n\n"
            f"Добро пожаловать в ESP Vision Bot!",
            parse_mode="HTML"
        )
        await cb.answer("✅ Верно!", show_alert=False)

        # Показываем главное меню
        await asyncio.sleep(1)
        user = get_user(uid)
        await show_main(cb.message, state, user, edit=False)

    else:
        # ❌ Неверный ответ
        attempts += 1
        await state.update_data(captcha_attempts=attempts)

        if attempts >= 3:
            # Слишком много попыток — новая капча
            await state.update_data(captcha_attempts=0)
            question, answer = generate_captcha()
            await state.update_data(captcha_answer=answer)

            await cb.message.edit_text(
                "❌ <b>Неверно! Слишком много ошибок.</b>\n\n"
                f"Новый пример: <b>{question} = ?</b>\n\n"
                "<i>Выбери правильный ответ:</i>",
                parse_mode="HTML",
                reply_markup=generate_captcha_keyboard(answer)
            )
            await cb.answer("❌ Неверно! Новый пример.", show_alert=True)
        else:
            # Та же капча, но перемешиваем кнопки заново
            correct = data.get("captcha_answer")
            question_hint = f"Попытка {attempts}/3"

            await cb.message.edit_text(
                f"❌ <b>Неверно!</b> Попробуй ещё раз.\n\n"
                f"<i>{question_hint}</i>\n\n"
                f"Реши пример чтобы продолжить:",
                parse_mode="HTML",
                reply_markup=generate_captcha_keyboard(correct)
            )
            await cb.answer("❌ Неверный ответ!", show_alert=False)


# ── Главное меню ──────────────────────────────────────────────
async def show_main(target, state: FSMContext, user: dict, edit=False):
    vip  = "👑 VIP" if is_vip(user) else "👤 Обычный"
    left = get_videos_left(user)
    text = (
        f"👁 <b>ESP Vision Bot</b>\n\n"
        f"Статус: <b>{vip}</b>\n"
        f"🎬 Осталось видео сегодня: <b>{left}</b>\n\n"
        f"Выберите действие:"
    )
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=kb_main())
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb_main())
    await state.set_state(S.main)

@dp.callback_query(F.data == "go_main")
async def go_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main(cb.message, state, get_user(cb.from_user.id), edit=True)
    await cb.answer()

@dp.callback_query(F.data == "go_mode")
async def go_mode(cb: CallbackQuery, state: FSMContext):
    user = get_user(cb.from_user.id)
    left = get_videos_left(user)
    if left <= 0:
        await cb.message.edit_text(
            f"⛔ <b>Лимит исчерпан!</b>\n\n"
            f"Лимит: <b>{get_daily_limit(user)} видео/день</b>\n\n"
            f"Пополни лимит:",
            parse_mode="HTML", reply_markup=kb_limit()
        )
        await cb.answer("❌ Лимит видео исчерпан!", show_alert=True)
        return
    await cb.message.edit_text(
        f"🎮 <b>Выбери тип ESP</b>\n\n"
        f"📦 <b>3D Box</b> — CS2-стиль объёмная рамка\n"
        f"⬜ <b>2D Box</b> — угловые линии как в CS/Valorant\n"
        f"🦴 <b>Скелет</b> — кости внутри персонажа\n\n"
        f"🎬 Осталось: <b>{left}</b>",
        parse_mode="HTML", reply_markup=kb_mode()
    )
    await state.set_state(S.mode)
    await cb.answer()

@dp.callback_query(F.data.startswith("esp_"), S.mode)
async def pick_esp(cb: CallbackQuery, state: FSMContext):
    mode  = cb.data
    names = {"esp_2d":"⬜ 2D Box","esp_3d":"📦 3D Box","esp_bones":"🦴 Скелет"}
    await state.update_data(esp_mode=mode)
    await state.set_state(S.video)
    await cb.message.edit_text(
        f"✅ Режим: <b>{names[mode]}</b>\n\n"
        f"📹 Отправь видео (до 50 МБ)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Сменить режим",
                                 callback_data="go_mode", style="primary")
        ]])
    )
    await cb.answer()

# ── VIP ───────────────────────────────────────────────────────
@dp.callback_query(F.data == "go_vip")
async def go_vip(cb: CallbackQuery):
    user  = get_user(cb.from_user.id)
    extra = f"\n\n✅ <b>VIP активен до {user['vip_until']}</b>" if is_vip(user) else ""
    await cb.message.edit_text(
        f"👑 <b>VIP подписка</b>\n\n"
        f"┌ <b>Обычный</b>\n│ • 1 видео/день\n└ бесплатно\n\n"
        f"┌ <b>👑 VIP</b>\n│ • 10 видео/день\n│ • Приоритет\n└ <b>25 ⭐ / месяц</b>"
        f"{extra}",
        parse_mode="HTML", reply_markup=kb_vip()
    )
    await cb.answer()

@dp.callback_query(F.data == "buy_vip")
async def buy_vip(cb: CallbackQuery):
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="👑 VIP ESP Vision — 30 дней",
        description="10 видео/день, все режимы ESP",
        payload=f"vip_{cb.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label="VIP 30 дней", amount=25)],
        provider_token="",
    )
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await pcq.answer(ok=True)

@dp.message(F.successful_payment)
async def on_payment(message: Message, state: FSMContext):
    user  = get_user(message.from_user.id)
    base  = (datetime.strptime(user["vip_until"],"%Y-%m-%d").date()
             if is_vip(user) else date.today())
    until = base + timedelta(days=30)
    user["vip_until"] = until.isoformat()
    save_user(user)
    await message.answer(
        f"🎉 <b>VIP активирован!</b>\n\n"
        f"📅 До: <b>{until.strftime('%d.%m.%Y')}</b>\n"
        f"🎬 Видео/день: <b>10</b>",
        parse_mode="HTML", reply_markup=kb_main()
    )
    await state.set_state(S.main)

# ── Реферал ───────────────────────────────────────────────────
@dp.callback_query(F.data == "go_ref")
async def go_ref(cb: CallbackQuery):
    user      = get_user(cb.from_user.id)
    uid       = cb.from_user.id
    ref_count = len(user.get("referrals", []))
    extra     = user.get("extra_videos", 0)
    ref_link  = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    await cb.message.edit_text(
        f"👥 <b>Реферальная система</b>\n\n"
        f"За каждого приглашённого друга:\n"
        f"• 🎬 <b>+1 видео тебе</b>\n"
        f"• 🎬 <b>+1 видео другу</b>\n"
        f"• ✅ Друг должен пройти проверку на бота\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Приглашено: <b>{ref_count}</b> чел.\n"
        f"• Бонусов: <b>{extra}</b> видео\n\n"
        f"🔗 <b>Твоя ссылка:</b>\n"
        f"<code>{ref_link}</code>",
        parse_mode="HTML", reply_markup=kb_ref(uid)
    )
    await cb.answer()

# ── Видео ─────────────────────────────────────────────────────
@dp.message(S.video, F.video | F.document)
async def on_video(message: Message, state: FSMContext):
    data     = await state.get_data()
    esp_mode = data.get("esp_mode","esp_2d")
    user     = get_user(message.from_user.id)

    if not use_video(user):
        await message.answer(
            f"⛔ <b>Лимит исчерпан!</b>\n"
            f"Лимит: <b>{get_daily_limit(user)} видео/день</b>",
            parse_mode="HTML", reply_markup=kb_limit()
        )
        return

    if message.video:
        file_id,size=message.video.file_id, message.video.file_size or 0
    else:
        file_id,size=message.document.file_id, message.document.file_size or 0

    if size > 50*1024*1024:
        await message.answer("❌ Файл больше 50 МБ")
        return

    prog = await message.answer("⬇️ <b>Скачиваю...</b>", parse_mode="HTML")

    with tempfile.TemporaryDirectory() as tmp:
        inp = os.path.join(tmp,"in.mp4")
        out = os.path.join(tmp,"out.mp4")
        try:
            f = await bot.get_file(file_id)
            await bot.download_file(f.file_path, inp)
        except Exception as e:
            await prog.edit_text(f"❌ Ошибка скачивания: {e}")
            return
        await prog.edit_text(
            "⚙️ <b>Обработка...</b>\n\n<code>[░░░░░░░░░░]</code> 0%",
            parse_mode="HTML"
        )
        try:
            await process_video(inp, out, esp_mode, prog)
        except Exception as e:
            logger.exception("process_video error")
            await prog.edit_text(f"❌ Ошибка: {e}")
            return
        await prog.edit_text("📤 <b>Отправляю...</b>", parse_mode="HTML")
        left = get_videos_left(user)
        try:
            await message.answer_video(
                FSInputFile(out, filename="esp_output.mp4"),
                caption=f"✅ <b>Готово!</b>  🎬 Осталось: <b>{left}</b>",
                parse_mode="HTML", reply_markup=kb_after()
            )
        except Exception:
            await message.answer_document(
                FSInputFile(out, filename="esp_output.mp4"),
                caption=f"✅ <b>Готово!</b>  🎬 Осталось: <b>{left}</b>",
                parse_mode="HTML", reply_markup=kb_after()
            )
        try:
            await prog.delete()
        except Exception:
            pass
    await state.set_state(S.mode)

@dp.message(S.video)
async def video_wrong(message: Message):
    await message.answer("⚠️ Отправь видео файл!")

@dp.message()
async def fallback(message: Message, state: FSMContext):
    await state.clear()
    await cmd_start(message, state)


# ═══════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════
async def main():
    logger.info("🚀 Запуск...")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())