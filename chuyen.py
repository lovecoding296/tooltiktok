# pip install requests send2trash
import os, re, json, time, threading, queue, mimetypes, requests
import tkinter as tk
from tkinter import filedialog, scrolledtext
from concurrent.futures import ThreadPoolExecutor
from bisect import bisect_left
from send2trash import send2trash

UPLOAD_URL = "http://{ip}/upload.json"
UPLOAD_FIELD = "files[]"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".avi")
PER_PHONE = 3

# cursor: TÊN folder tiếp theo | sent: gửi xong nhưng không bỏ được vào Thùng rác (tránh gửi lại)
state = {"root": "", "ips": [], "cursor": "", "sent": {}}
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    except Exception:
        pass
if not isinstance(state["cursor"], str):
    state["cursor"] = ""

EMPTY = set()
lock = threading.Lock()        # chặn bấm CHUYỂN 2 lần cùng lúc
save_lock = threading.Lock()   # chỉ 1 luồng ghi state.json tại một thời điểm
logq = queue.Queue()

def nat(s):
    """Khoá sắp xếp tự nhiên: 'a-2' < 'a-10'."""
    return [int(p) if i % 2 else p for i, p in enumerate(re.split(r"(\d+)", s.lower()))]

def log(msg):
    logq.put(msg)

def save_state():
    """Ghi state.json an toàn: file tạm + os.replace, thử lại khi bị khoá. Không bao giờ văng lỗi."""
    with save_lock:
        tmp = STATE_FILE + ".tmp"
        err = None
        for _ in range(6):
            try:
                data = json.dumps(state, ensure_ascii=False, indent=2)
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, STATE_FILE)
                return True
            except (OSError, RuntimeError) as e:
                err = e
                time.sleep(0.3)
        log(f"CẢNH BÁO: không ghi được {STATE_FILE}: {err}. "
            f"Đóng chương trình đang mở file đó, hoặc chuyển tool ra thư mục không đồng bộ OneDrive.")
        return False

def pump_log():
    while not logq.empty():
        txt.insert(tk.END, logq.get() + "\n"); txt.see(tk.END)
    win.after(200, pump_log)

def get_ips():
    out = []
    for line in ips_box.get("1.0", tk.END).splitlines():
        s = re.sub(r"^https?://", "", line.strip()).strip("/")
        if s:
            out.append(s)
    return out

def subfolders():
    with os.scandir(state["root"]) as it:
        return sorted((e.name for e in it if e.is_dir()), key=nat)

def available(folder):
    sent = set(state["sent"].get(folder, []))
    try:
        with os.scandir(os.path.join(state["root"], folder)) as it:
            names = sorted((e.name for e in it
                            if e.is_file() and e.name.lower().endswith(VIDEO_EXT)
                            and e.name not in sent), key=nat)
    except OSError:
        return []
    return names[:PER_PHONE]

def pick(order, need):
    picked = []
    for pos, folder in enumerate(order):
        if len(picked) >= need:
            break
        if folder in EMPTY:
            continue
        files = available(folder)
        if files:
            picked.append((pos, folder, files))
        else:
            EMPTY.add(folder)
    return picked

def upload_one(ip, folder, files):
    ok = 0
    for name in files:
        path = os.path.join(state["root"], folder, name)
        try:
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                r = requests.post(UPLOAD_URL.format(ip=ip),
                                  files={UPLOAD_FIELD: (name, f, ctype)},
                                  headers={"Referer": f"http://{ip}/"},
                                  timeout=(10, 1800))
            if r.status_code != 200:
                log(f"[{ip}] LỖI {r.status_code}  {folder}/{name} (giữ lại trên PC)")
                continue
            ok += 1
            try:
                send2trash(os.path.abspath(path))
                log(f"[{ip}] OK + vào Thùng rác: {folder}/{name}")
            except Exception as e:
                state["sent"].setdefault(folder, []).append(name)
                log(f"[{ip}] OK nhưng không bỏ được vào Thùng rác {folder}/{name}: {e}")
        except Exception as e:
            log(f"[{ip}] LỖI {folder}/{name}: {e} (giữ lại trên PC)")
    return folder, ok

def do_send():
    if not lock.acquire(blocking=False):
        return log("Đang chạy, đợi xong đã")
    try:
        state["root"] = root_var.get(); state["ips"] = get_ips()
        save_state()                                   # ghi lần 1: ngay khi bấm CHUYỂN
        try:
            folders = subfolders()
        except Exception as e:
            return log(f"Lỗi folder cha: {e}")
        if not folders or not state["ips"]:
            return log("Chưa có folder con hoặc IP")

        keys = [nat(f) for f in folders]
        start = bisect_left(keys, nat(state["cursor"])) % len(folders)
        order = folders[start:] + folders[:start]
        picked = pick(order, len(state["ips"]))
        if not picked and EMPTY:
            EMPTY.clear()
            picked = pick(order, len(state["ips"]))
        if not picked:
            return log("Hết video ở tất cả folder")

        last_pos = picked[-1][0]
        if start > 0 and last_pos >= len(folders) - start:
            log("(Đã quay lại đầu danh sách folder)")
        state["cursor"] = order[(last_pos + 1) % len(order)]
        save_state()                                   # ghi lần 2: ngay sau khi chọn folder

        jobs = [(ip, folder, files)
                for ip, (_, folder, files) in zip(state["ips"], picked)]
        log(f"Folder: {jobs[0][1]} ... {jobs[-1][1]} ({len(jobs)} folder) | tiếp theo: {state['cursor']}")

        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            results = list(ex.map(lambda j: upload_one(*j), jobs))

        for (_, folder, _), (_, ok) in zip(picked, results):
            if ok == 0:                    # folder không gửi được video nào
                state["cursor"] = folder   # lần sau thử lại từ folder này
                break
        log(f"=== Xong: {sum(r[1] for r in results)}/{sum(len(j[2]) for j in jobs)} video thành công ===")
    finally:
        try:
            save_state()                               # ghi lần 3: kết thúc (kể cả khi lỗi)
        finally:
            lock.release()

def reset_all():
    state["cursor"] = ""; state["sent"] = {}; EMPTY.clear()
    save_state(); log("Đã reset: con trỏ về folder đầu, xoá lịch sử đã gửi")

def run(fn):
    threading.Thread(target=fn, daemon=True).start()

# ===== GUI =====
win = tk.Tk(); win.title("VLC iPhone Sender"); win.geometry("720x560")
root_var = tk.StringVar(value=state["root"])
top = tk.Frame(win); top.pack(fill="x", padx=8, pady=6)
tk.Entry(top, textvariable=root_var).pack(side="left", fill="x", expand=True)
tk.Button(top, text="Chọn folder cha",
          command=lambda: root_var.set(filedialog.askdirectory() or root_var.get())).pack(side="left", padx=4)

tk.Label(win, text="Danh sách IP iPhone (mỗi dòng 1 IP):").pack(anchor="w", padx=8)
ips_box = tk.Text(win, height=7); ips_box.pack(fill="x", padx=8)
ips_box.insert("1.0", "\n".join(state["ips"]))

btns = tk.Frame(win); btns.pack(pady=6)
tk.Button(btns, text="CHUYỂN", width=14, bg="#4caf50", fg="white", command=lambda: run(do_send)).pack(side="left", padx=4)
tk.Button(btns, text="Reset", command=reset_all).pack(side="left", padx=4)

txt = scrolledtext.ScrolledText(win); txt.pack(fill="both", expand=True, padx=8, pady=6)
pump_log()
win.mainloop()