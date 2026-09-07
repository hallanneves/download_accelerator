#!/usr/bin/env python3
"""
Interface grafica do acelerador de download, no estilo do macOS.

    python3 download_accelerator_gui.py [URL] [--start]

Precisa do download_accelerator.py na mesma pasta (e o motor).
Usa Tkinter, que ja vem com o Python. No macOS com Python do Homebrew:
    brew install python-tk@3.14
Ou rode com o Python da Apple, que ja tem Tkinter:
    /usr/bin/python3 download_accelerator_gui.py

Memoria: a lista de downloads fica em
    ~/Library/Application Support/Download Accelerator/history.json  (macOS)
    ~/.download_accelerator/history.json                              (outros)
e o progresso de cada download interrompido fica em <arquivo>.part.json,
ao lado do <arquivo>.part, para poder retomar.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk
except ImportError:
    sys.exit(
        "Tkinter nao encontrado neste Python.\n"
        "  macOS (Homebrew): brew install python-tk@3.14\n"
        "  ou rode: /usr/bin/python3 download_accelerator_gui.py"
    )

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download_accelerator as dl  # noqa: E402

APP_NAME = "Download Accelerator"
POLL_MS = 200
SPEED_WINDOW_S = 3.0
MAX_BINS = 160          # blocos desenhados no mapa de pedacos
HISTORY_ROWS = 6        # linhas mostradas na lista de downloads


# --------------------------------------------------------------------------- #
# Memoria: lista de downloads (a retomada em si usa o .part.json do motor)
# --------------------------------------------------------------------------- #
def history_path():
    if sys.platform == "darwin":
        base = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    elif os.name == "nt":
        base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
    else:
        base = os.path.expanduser("~/.download_accelerator")
    return os.path.join(base, "history.json")


class History:
    """Entradas por caminho de saida: url, total, status, updated, error."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.entries = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.entries = {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.entries = {}

    def _save_locked(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def upsert(self, output, **fields):
        with self._lock:
            e = self.entries.setdefault(output, {"output": output})
            e.update(fields)
            e["updated"] = time.time()
            self._save_locked()

    def remove(self, output):
        with self._lock:
            self.entries.pop(output, None)
            self._save_locked()

    def reconcile(self):
        """Ao abrir o app: 'downloading' virou interrompido; entradas sem arquivo somem."""
        with self._lock:
            for out, e in list(self.entries.items()):
                part = out + ".part"
                if e.get("status") == "downloading":
                    e["status"] = "interrupted"
                if e.get("status") == "interrupted" and not os.path.exists(part):
                    if os.path.exists(out):
                        e["status"] = "done"
                    else:
                        del self.entries[out]
                elif e.get("status") == "done" and not os.path.exists(out):
                    del self.entries[out]
            self._save_locked()

    def sorted(self):
        with self._lock:
            return sorted(self.entries.values(), key=lambda e: e.get("updated", 0), reverse=True)


# --------------------------------------------------------------------------- #
# Aparencia: cores do sistema quando o Tk expoe, senao valores das HIG da Apple
# --------------------------------------------------------------------------- #
def is_dark(root):
    try:
        return bool(int(root.tk.call("::tk::unsupported::MacWindowStyle", "isdark", root)))
    except tk.TclError:
        pass
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                             capture_output=True, text=True, timeout=2)
        return out.stdout.strip().lower() == "dark"
    except Exception:  # noqa: BLE001
        return False


def sys_color(root, name, fallback):
    try:
        r, g, b = root.winfo_rgb(name)
        return f"#{r >> 8:02x}{g >> 8:02x}{b >> 8:02x}"
    except tk.TclError:
        return fallback


def make_palette(root):
    dark = is_dark(root)
    if dark:
        p = {
            "window": sys_color(root, "systemWindowBackgroundColor", "#1e1e1e"),
            "card": "#29292b", "border": "#3a3a3c", "separator": "#38383a",
            "label": "#f5f5f7", "secondary": "#98989d", "tertiary": "#636366",
            "track": "#3a3a3c", "log_bg": "#1e1e1e",
            "green": "#30d158", "red": "#ff453a", "orange": "#ff9f0a",
        }
    else:
        p = {
            "window": sys_color(root, "systemWindowBackgroundColor", "#ececec"),
            "card": "#ffffff", "border": "#d8d8dc", "separator": "#e5e5ea",
            "label": "#1d1d1f", "secondary": "#6e6e73", "tertiary": "#aeaeb2",
            "track": "#e5e5ea", "log_bg": "#f5f5f7",
            "green": "#34c759", "red": "#ff3b30", "orange": "#ff9500",
        }
    p["accent"] = sys_color(root, "systemControlAccentColor", "#0a84ff" if dark else "#007aff")
    p["dark"] = dark
    return p


def make_fonts():
    base = tkfont.nametofont("TkDefaultFont")
    size = base.actual("size") or 13
    family = base.actual("family")
    mono = "SF Mono" if "SF Mono" in tkfont.families() else "Menlo"
    return {
        "body": tkfont.Font(family=family, size=size),
        "bold": tkfont.Font(family=family, size=size, weight="bold"),
        "small": tkfont.Font(family=family, size=max(size - 2, 8)),
        "big": tkfont.Font(family=family, size=size * 3, weight="bold"),
        "mono": tkfont.Font(family=mono, size=max(size - 2, 8)),
    }


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


# Apple mostra tamanhos em base 10 (1 MB = 1.000.000 bytes), como o Finder.
def fmt_size(n):
    n = float(n or 0)
    if n < 1000:
        return f"{int(n)} bytes"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1000
        if n < 1000 or unit == "TB":
            dec = 2 if unit in ("GB", "TB") else 1
            return f"{n:.{dec}f}".replace(".", ",") + f" {unit}"


def fmt_remaining(s):
    if s is None or s == float("inf") or s != s:
        return "Calculando o tempo restante"
    s = int(s)
    if s < 60:
        return f"{s} s restantes"
    m, _ = divmod(s, 60)
    if m < 60:
        return f"{m} min restantes"
    h, m = divmod(m, 60)
    return f"{h} h {m} min restantes"


def fmt_elapsed(s):
    s = int(s)
    if s < 60:
        return f"{s} s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} min {sec} s"
    h, m = divmod(m, 60)
    return f"{h} h {m} min"


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


# --------------------------------------------------------------------------- #
# Cartao arredondado (agrupamento no estilo dos Ajustes do macOS)
# --------------------------------------------------------------------------- #
class Card(tk.Canvas):
    def __init__(self, parent, palette, pad=16, radius=12):
        super().__init__(parent, bg=palette["window"], highlightthickness=0, bd=0)
        self.p, self.pad, self.radius = palette, pad, radius
        self._height = 0
        self.body = tk.Frame(self, bg=palette["card"])
        self._win = self.create_window(pad, pad, window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._fit)
        self.bind("<Configure>", self._redraw)

    def _fit(self, _e=None):
        h = self.body.winfo_reqheight() + 2 * self.pad
        if h != self._height:  # cget("height") devolve "7c" por padrao, por isso o cache
            self._height = h
            self.configure(height=h)

    def _redraw(self, _e=None):
        w, h = self.winfo_width(), self.winfo_height()
        self.itemconfigure(self._win, width=max(w - 2 * self.pad, 1))
        self.delete("bg")
        round_rect(self, 1, 1, w - 1, h - 1, self.radius,
                   fill=self.p["card"], outline=self.p["border"], tags="bg")
        self.tag_lower("bg")


# --------------------------------------------------------------------------- #
# Aplicacao
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root, url="", autostart=False):
        self.root = root
        self.root.title("Acelerador de Download")
        self.p = make_palette(root)
        self.f = make_fonts()
        self.root.configure(bg=self.p["window"])
        self.root.minsize(640, 460)

        self.history = History(history_path())
        self.history.reconcile()

        self.events = queue.Queue()
        self.dl = None
        self.cancel = None
        self.progress = None
        self.info = None
        self.worker = None
        self.samples = deque()
        self.state = "idle"        # idle | probing | downloading | hashing | done | error | cancelled
        self.hash_done = 0
        self.final_path = None
        self.details_open = False
        self._last_conn_applied = None

        self._build(url)
        self._render_history()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.root.createcommand("::tk::mac::Quit", self._on_close)  # Cmd+Q tambem salva
        except tk.TclError:
            pass
        self._poll_id = self.root.after(POLL_MS, self._poll)
        if autostart and url:
            self.root.after(100, self.start)

    # ------------------------------------------------------------------ UI
    def _label(self, parent, text, font="body", color="label", **kw):
        return tk.Label(parent, text=text, font=self.f[font], fg=self.p[color],
                        bg=parent.cget("bg"), **kw)

    def _build(self, url):
        p = self.p
        outer = tk.Frame(self.root, bg=p["window"])
        outer.pack(fill="both", expand=True, padx=20, pady=(18, 16))
        self.outer = outer

        # ---- Cartao 1: o que baixar -------------------------------------
        form = Card(outer, p)
        form.pack(fill="x")
        body = form.body
        body.columnconfigure(1, weight=1)
        rowpad = {"pady": 6}

        self._label(body, "Link", anchor="e", width=9).grid(row=0, column=0, sticky="e", padx=(0, 10), **rowpad)
        self.url_var = tk.StringVar(value=url)
        url_entry = ttk.Entry(body, textvariable=self.url_var, font=self.f["body"])
        url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", **rowpad)
        url_entry.bind("<Return>", lambda _e: self.start())

        self._label(body, "Salvar em", anchor="e", width=9).grid(row=1, column=0, sticky="e", padx=(0, 10), **rowpad)
        self.dir_var = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        ttk.Entry(body, textvariable=self.dir_var, font=self.f["body"]).grid(row=1, column=1, sticky="ew", **rowpad)
        ttk.Button(body, text="Escolher…", command=self._choose_dir).grid(row=1, column=2, sticky="e", padx=(8, 0), **rowpad)

        self._label(body, "Conexões", anchor="e", width=9).grid(row=2, column=0, sticky="e", padx=(0, 10), **rowpad)
        conn_row = tk.Frame(body, bg=p["card"])
        conn_row.grid(row=2, column=1, columnspan=2, sticky="w", **rowpad)
        self.conn_var = tk.IntVar(value=8)
        spin = ttk.Spinbox(conn_row, from_=1, to=64, width=4, textvariable=self.conn_var,
                           font=self.f["body"], command=self._conn_changed)
        spin.pack(side="left")
        spin.bind("<KeyRelease>", lambda _e: self._conn_changed())
        spin.bind("<FocusOut>", lambda _e: self._conn_changed())
        self.conn_hint = self._label(conn_row, "pedaços baixados ao mesmo tempo. Pode mudar durante o download.",
                                     font="small", color="secondary")
        self.conn_hint.pack(side="left", padx=(10, 0))

        self._label(body, "SHA-256", anchor="e", width=9).grid(row=3, column=0, sticky="e", padx=(0, 10), **rowpad)
        self.hash_var = tk.StringVar()
        ttk.Entry(body, textvariable=self.hash_var, font=self.f["mono"]).grid(
            row=3, column=1, columnspan=2, sticky="ew", **rowpad)
        self._label(body, "Opcional. Confere o arquivo depois de baixar.", font="small",
                    color="secondary", anchor="w").grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 2))

        # ---- Cartao 2: progresso ----------------------------------------
        prog = Card(outer, p)
        prog.pack(fill="x", pady=(14, 0))
        pb = prog.body
        pb.columnconfigure(0, weight=1)
        pb.columnconfigure(1, weight=1)

        self.pct_var = tk.StringVar(value="0%")
        self.pct_lbl = self._label(pb, "", font="big", color="tertiary", anchor="w")
        self.pct_lbl.configure(textvariable=self.pct_var)
        self.pct_lbl.grid(row=0, column=0, sticky="w")

        self.size_var = tk.StringVar(value="Nenhum download em andamento")
        lbl = self._label(pb, "", color="secondary", anchor="e")
        lbl.configure(textvariable=self.size_var)
        lbl.grid(row=0, column=1, sticky="se", pady=(0, 6))

        self.bar = ttk.Progressbar(pb, mode="determinate", maximum=1000)
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 8))

        self.speed_var = tk.StringVar(value="")
        self.eta_var = tk.StringVar(value="")
        lbl = self._label(pb, "", font="small", color="secondary", anchor="w")
        lbl.configure(textvariable=self.speed_var)
        lbl.grid(row=2, column=0, sticky="w")
        lbl = self._label(pb, "", font="small", color="secondary", anchor="e")
        lbl.configure(textvariable=self.eta_var)
        lbl.grid(row=2, column=1, sticky="e")

        self.map = tk.Canvas(pb, height=12, bg=p["card"], highlightthickness=0, bd=0)
        self.map.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 2))
        self.map.bind("<Configure>", lambda _e: self._draw_map())

        # ---- Barra de acoes ---------------------------------------------
        actions = tk.Frame(outer, bg=p["window"])
        actions.pack(fill="x", pady=(16, 0))
        self.open_btn = ttk.Button(actions, text="Mostrar no Finder", command=self._reveal)
        # acao secundaria: fica a esquerda e so aparece quando houver arquivo pronto
        self.status_var = tk.StringVar(value="Cole um link e clique em Iniciar.")
        self.status_lbl = self._label(actions, "", color="secondary", anchor="w")
        self.status_lbl.configure(textvariable=self.status_var)
        self.status_lbl.pack(side="left", fill="x", expand=True)

        self.start_btn = ttk.Button(actions, text="Iniciar", default="active", command=self.start)
        self.start_btn.pack(side="right")
        self.cancel_btn = ttk.Button(actions, text="Cancelar", command=self.cancel_download, state="disabled")
        self.cancel_btn.pack(side="right", padx=(0, 8))

        # ---- Cartao 3: downloads (memoria) -------------------------------
        self.hist_card = Card(outer, p)
        hb = self.hist_card.body
        hb.columnconfigure(0, weight=1)
        head = tk.Frame(hb, bg=p["card"])
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self._label(head, "Downloads", font="bold").pack(side="left")
        self.clear_lbl = self._label(head, "Limpar concluídos", font="small", color="accent", cursor="hand2")
        self.clear_lbl.pack(side="right")
        self.clear_lbl.bind("<Button-1>", lambda _e: self._clear_done())
        self.hist_rows = tk.Frame(hb, bg=p["card"])
        self.hist_rows.grid(row=1, column=0, sticky="ew")
        self.hist_rows.columnconfigure(0, weight=1)
        # o cartao so e empacotado quando houver entradas (ver _render_history)

        # ---- Detalhes (registro), recolhido por padrao --------------------
        self.details = tk.Frame(outer, bg=p["window"])
        self.details.pack(fill="both", expand=True, pady=(14, 0))
        self.disclosure = self._label(self.details, "›  Detalhes", font="small", color="secondary",
                                      anchor="w", cursor="hand2")
        self.disclosure.pack(fill="x")
        self.disclosure.bind("<Button-1>", lambda _e: self._toggle_details())

        self.log = tk.Text(self.details, height=7, state="disabled", wrap="word", relief="flat",
                           bd=0, highlightthickness=0, padx=10, pady=8,
                           bg=p["log_bg"], fg=p["secondary"], insertbackground=p["label"],
                           font=self.f["mono"])

    # ---- lista de downloads ---------------------------------------------
    def _render_history(self):
        for w in self.hist_rows.winfo_children():
            w.destroy()
        entries = self.history.sorted()[:HISTORY_ROWS]
        if not entries:
            self.hist_card.pack_forget()
            return
        if not self.hist_card.winfo_manager():
            self.hist_card.pack(fill="x", pady=(14, 0), before=self.details)

        busy = self.state in ("probing", "downloading", "hashing")
        current = self.info["output"] if self.info else None
        for i, e in enumerate(entries):
            out = e["output"]
            if i:
                tk.Frame(self.hist_rows, bg=self.p["separator"], height=1).grid(
                    row=2 * i - 1, column=0, sticky="ew", pady=4)
            row = tk.Frame(self.hist_rows, bg=self.p["card"])
            row.grid(row=2 * i, column=0, sticky="ew", pady=2)
            row.columnconfigure(0, weight=1)

            text = tk.Frame(row, bg=self.p["card"])
            text.grid(row=0, column=0, sticky="w")
            self._label(text, os.path.basename(out), anchor="w").pack(anchor="w")
            status, color = self._describe_entry(e, active=(busy and out == current))
            self._label(text, status, font="small", color=color, anchor="w").pack(anchor="w")

            btns = tk.Frame(row, bg=self.p["card"])
            btns.grid(row=0, column=1, sticky="e", padx=(12, 0))
            st = e.get("status")
            if busy and out == current:
                pass  # e o download em andamento: sem acoes aqui
            elif st == "interrupted":
                ttk.Button(btns, text="Retomar", state="disabled" if busy else "normal",
                           command=lambda e=e: self._resume(e)).pack(side="left")
                ttk.Button(btns, text="Apagar", command=lambda e=e: self._discard(e)).pack(side="left", padx=(6, 0))
            elif st == "done":
                ttk.Button(btns, text="Mostrar no Finder",
                           command=lambda o=out: self._reveal(o)).pack(side="left")
                ttk.Button(btns, text="Remover",
                           command=lambda o=out: self._forget(o)).pack(side="left", padx=(6, 0))
            else:
                ttk.Button(btns, text="Remover",
                           command=lambda o=out: self._forget(o)).pack(side="left")

    def _describe_entry(self, e, active=False):
        total = e.get("total")
        st = e.get("status")
        if active:
            return "Baixando agora", "accent"
        if st == "done":
            return f"Concluído, {fmt_size(total)}" if total else "Concluído", "secondary"
        if st == "interrupted":
            saved = dl.load_state(e["output"] + ".part")
            done = saved.get("done", 0) if saved else 0
            pct = int(done / total * 100) if total else 0
            head = "Falhou" if e.get("error") else "Interrompido"
            if total:
                return f"{head} em {pct}%, {fmt_size(done)} de {fmt_size(total)}", "orange"
            return head, "orange"
        return st or "", "secondary"

    def _resume(self, e):
        if self.state in ("probing", "downloading", "hashing"):
            return
        self.url_var.set(e.get("url", ""))
        self.dir_var.set(os.path.dirname(e["output"]) or os.path.expanduser("~/Downloads"))
        self.hash_var.set(e.get("sha256", "") or "")
        self.start()

    def _discard(self, e):
        name = os.path.basename(e["output"])
        if not messagebox.askyesno("Apagar download", f"Apagar o que já foi baixado de “{name}”?\n"
                                   "Isso não pode ser desfeito.", parent=self.root):
            return
        dl.discard_partial(e["output"])
        self.history.remove(e["output"])
        self._log(f"Parcial apagado: {name}")
        self._render_history()

    def _forget(self, out):
        self.history.remove(out)
        self._render_history()

    def _clear_done(self):
        for e in self.history.sorted():
            if e.get("status") == "done":
                self.history.remove(e["output"])
        self._render_history()

    # ---- pequenos controles ---------------------------------------------
    def _toggle_details(self, force=None):
        want = (not self.details_open) if force is None else force
        if want == self.details_open:
            return
        self.details_open = want
        if want:
            self.disclosure.configure(text="⌄  Detalhes")
            self.log.pack(fill="both", expand=True, pady=(6, 0))
        else:
            self.disclosure.configure(text="›  Detalhes")
            self.log.pack_forget()

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if d:
            self.dir_var.set(d)

    def _reveal(self, path=None):
        path = path or self.final_path
        if not path:
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            elif os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:  # noqa: BLE001
            self._log(f"Nao consegui abrir a pasta: {e}")

    def _set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")

    def _show_open_button(self, show):
        if show:
            self.open_btn.pack(side="left", padx=(0, 12), before=self.status_lbl)
        else:
            self.open_btn.pack_forget()

    def _read_connections(self):
        try:
            return max(1, min(64, int(self.conn_var.get())))
        except (tk.TclError, ValueError):
            return None

    def _conn_changed(self):
        n = self._read_connections()
        if n is None or self.dl is None or self.state != "downloading":
            return
        if n == self._last_conn_applied:
            return
        self._last_conn_applied = n
        self.dl.set_connections(n)
        verb = "Retomando" if self.info and self.info.get("resumed") else "Baixando"
        self.status_var.set(f"{verb} com {plural(n, 'conexão', 'conexões')}…")
        self._log(f"Conexões ajustadas para {n}")

    # -------------------------------------------------------------- actions
    def start(self):
        url = self.url_var.get().strip()
        if not url:
            self.status_var.set("Cole um link para começar.")
            return
        if self.state in ("probing", "downloading", "hashing"):
            return
        out_dir = self.dir_var.get().strip() or os.path.expanduser("~/Downloads")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            self.status_var.set(f"Não foi possível usar a pasta: {e}")
            return
        connections = self._read_connections() or 8
        self.conn_var.set(connections)
        self._last_conn_applied = connections

        self.cancel = threading.Event()
        self.progress = None
        self.info = None
        self.final_path = None
        self.samples.clear()
        self.hash_done = 0
        self.state = "probing"
        self.bar["value"] = 0
        self.pct_var.set("0%")
        self.pct_lbl.configure(fg=self.p["tertiary"])
        self.size_var.set("Consultando o servidor")
        self.speed_var.set("")
        self.eta_var.set("")
        self._show_open_button(False)
        self._set_running(True)
        self.status_var.set("Consultando o servidor…")
        self._log(f"Consultando {url}")
        self._draw_map()

        expected_hash = self.hash_var.get().strip() or None
        self.dl = dl.Download(url, out_dir, connections=connections, cancel=self.cancel,
                              on_start=lambda pr, info: self.events.put(("start", pr, info)))
        self.worker = threading.Thread(target=self._run, args=(url, expected_hash), daemon=True)
        self.worker.start()

    def cancel_download(self):
        if self.cancel and self.state in ("probing", "downloading"):
            self.cancel.set()
            self.status_var.set("Cancelando e salvando o progresso…")
            self.cancel_btn.configure(state="disabled")

    def _on_close(self):
        if self.state in ("probing", "downloading") and self.cancel:
            # para as conexoes e deixa o motor gravar o estado antes de sair
            self.cancel.set()
            self.status_var.set("Salvando o progresso…")
            self.root.update_idletasks()
            if self.worker:
                self.worker.join(timeout=8)
        if self._poll_id:
            self.root.after_cancel(self._poll_id)
            self._poll_id = None
        self.root.destroy()

    # --------------------------------------------------------------- worker
    def _run(self, url, expected_hash):
        d = self.dl
        try:
            path = d.run()
        except dl.DownloadCancelled as e:
            if d.info:
                self.history.upsert(d.info["output"], url=url, total=d.info["total"],
                                    status="interrupted", error=None, sha256=expected_hash)
            self.events.put(("cancelled", e.part_path))
            return
        except dl.DownloadError as e:
            if d.info:
                self.history.upsert(d.info["output"], url=url, total=d.info["total"],
                                    status="interrupted", error=str(e), sha256=expected_hash)
            self.events.put(("error", str(e)))
            return
        except Exception as e:  # noqa: BLE001
            self.events.put(("error", f"{type(e).__name__}: {e}"))
            return

        if expected_hash:
            self.events.put(("hashing", path))

            def on_hash(n):
                self.hash_done = n

            got = dl.sha256_of(path, on_hash)
            if got.lower() != expected_hash.lower():
                self.history.upsert(path, url=url, total=d.info["total"], status="done",
                                    error="SHA-256 diferente", sha256=expected_hash)
                self.events.put(("error", f"O SHA-256 não confere.\n"
                                          f"          esperado: {expected_hash}\n"
                                          f"          obtido:   {got}"))
                return
            self.events.put(("hash_ok", got))

        self.history.upsert(path, url=url, total=d.info["total"], status="done",
                            error=None, sha256=expected_hash)
        self.events.put(("done", path))

    # ----------------------------------------------------------------- poll
    def _poll(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass

        if self.state == "downloading" and self.progress is not None:
            self._update_stats()
            self._draw_map()
        elif self.state == "hashing" and self.info and self.info["total"]:
            frac = self.hash_done / self.info["total"]
            self.bar["value"] = frac * 1000
            self.pct_var.set(f"{int(frac * 100)}%")

        if self.root.winfo_exists():
            self._poll_id = self.root.after(POLL_MS, self._poll)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "start":
            _, self.progress, self.info = ev
            self.state = "downloading"
            total = self.info["total"]
            self.history.upsert(self.info["output"], url=self.url_var.get().strip(), total=total,
                                status="downloading", error=None,
                                sha256=self.hash_var.get().strip() or None)
            self.pct_lbl.configure(fg=self.p["label"])
            if self.info["parallel"]:
                mode = (f"{plural(self.dl.target_connections, 'conexão', 'conexões')}, "
                        f"{self.info['chunks']} pedaços de {fmt_size(self.info['chunk_size'])}")
            else:
                why = ("o servidor não aceita download em partes" if not self.info["ranges_ok"]
                       else "arquivo pequeno ou de tamanho desconhecido")
                mode = f"1 conexão, {why}"
            self._log(f"Arquivo: {self.info['output']}")
            self._log(f"Tamanho: {fmt_size(total) if total else 'desconhecido'}. {mode[0].upper() + mode[1:]}.")
            if self.info["resumed"]:
                self._log(f"Retomando: {fmt_size(self.info['resumed_bytes'])} já estavam baixados.")
                self.status_var.set(f"Retomando com {mode.split(',')[0]}…")
            else:
                self.status_var.set(f"Baixando com {mode.split(',')[0]}…")
            self._update_stats()
            self._render_history()
        elif kind == "hashing":
            self.state = "hashing"
            self.status_var.set("Verificando o SHA-256…")
            self._log("Verificando o SHA-256…")
            self.speed_var.set("")
            self.eta_var.set("Verificando o arquivo")
        elif kind == "hash_ok":
            self._log(f"SHA-256 confere: {ev[1]}")
        elif kind == "done":
            self.state = "done"
            self.final_path = ev[1]
            self.bar["value"] = 1000
            self.pct_var.set("100%")
            self.pct_lbl.configure(fg=self.p["green"])
            if self.progress:
                self._update_stats(final=True)
                self._draw_map()
            self.status_var.set(f"Arquivo salvo: {os.path.basename(ev[1])}")
            self._log(f"Salvo em: {os.path.abspath(ev[1])}")
            self._show_open_button(True)
            self._set_running(False)
            self._render_history()
        elif kind == "cancelled":
            self.state = "cancelled"
            self.pct_lbl.configure(fg=self.p["tertiary"])
            self.status_var.set("Download interrompido. Dá para retomar na lista abaixo.")
            self.speed_var.set("")
            self.eta_var.set("Interrompido")
            if self.progress:
                self._draw_map()
            self._log(f"Interrompido. O progresso ficou salvo em: {ev[1]}")
            self._set_running(False)
            self._render_history()
        elif kind == "error":
            self.state = "error"
            self.pct_lbl.configure(fg=self.p["red"])
            self.status_var.set("O download falhou. Veja os detalhes.")
            self.speed_var.set("")
            self.eta_var.set("Falhou")
            self._log("Erro: " + ev[1])
            self._toggle_details(True)
            self._set_running(False)
            if self.progress:
                self._draw_map()
            self._render_history()

    def _update_stats(self, final=False):
        done, _, _ = self.progress.snapshot()
        total = self.progress.total
        now = time.monotonic()
        self.samples.append((now, done))
        while self.samples and now - self.samples[0][0] > SPEED_WINDOW_S:
            self.samples.popleft()

        if final:
            speed = self.progress.session_done / max(self.progress.elapsed, 1e-6)
        elif len(self.samples) >= 2:
            (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
            speed = (b1 - b0) / max(t1 - t0, 1e-6)
        else:
            speed = 0.0

        if final:
            self.speed_var.set(f"Média de {fmt_size(speed)}/s")
            self.eta_var.set(f"Concluído em {fmt_elapsed(self.progress.elapsed)}")
            if total:
                self.size_var.set(fmt_size(total))
            return

        conns = self.dl.connections if self.dl else 0
        if speed > 0:
            self.speed_var.set(f"{fmt_size(speed)}/s com {plural(conns, 'conexão', 'conexões')}")
        else:
            self.speed_var.set(plural(conns, "conexão aberta", "conexões abertas") if conns else "")
        if total:
            frac = done / total
            self.bar["value"] = frac * 1000
            self.pct_var.set(f"{int(frac * 100)}%")
            self.size_var.set(f"{fmt_size(done)} de {fmt_size(total)}")
            eta = (total - done) / speed if speed > 0 else None
            self.eta_var.set(fmt_remaining(eta))
        else:
            self.size_var.set(fmt_size(done))

    def _draw_map(self):
        c = self.map
        c.delete("all")
        w = max(c.winfo_width(), 1)
        h = c.winfo_height()
        if not self.progress or not self.info or not self.info["parallel"]:
            round_rect(c, 0, 0, w, h, min(h / 2, 6), fill=self.p["track"], outline="")
            return
        _, chunk_done, chunk_state = self.progress.snapshot()
        n = len(chunk_done)
        if n == 0:
            return
        total, csize = self.info["total"], self.info["chunk_size"]
        sizes = [min(csize, total - i * csize) for i in range(n)]

        bins = min(n, MAX_BINS)
        per_bin = n / bins
        gap = 2 if w / bins >= 5 else 1
        bw = (w - gap * (bins - 1)) / bins
        r = min(bw / 2, h / 2, 3)

        for b in range(bins):
            lo, hi = int(b * per_bin), int((b + 1) * per_bin)
            hi = max(hi, lo + 1)
            got = sum(chunk_done[lo:hi])
            size = sum(sizes[lo:hi])
            states = chunk_state[lo:hi]
            x0 = b * (bw + gap)
            x1 = x0 + bw
            round_rect(c, x0, 0, x1, h, r, fill=self.p["track"], outline="")
            if dl.FAILED in states:
                color, frac = self.p["red"], 1.0
            elif all(s == dl.DONE for s in states):
                color, frac = self.p["green"], 1.0
            elif any(s == dl.ACTIVE for s in states):
                color, frac = self.p["accent"], (got / size if size else 0)
            elif got:
                color, frac = self.p["green"], (got / size if size else 0)  # parte retomada, ainda parada
            else:
                continue
            fx1 = x0 + bw * frac
            if fx1 - x0 >= 1:
                round_rect(c, x0, 0, fx1, h, min(r, (fx1 - x0) / 2), fill=color, outline="")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    autostart = "--start" in sys.argv
    url = args[0] if args else ""

    root = tk.Tk()
    try:
        ttk.Style().theme_use("aqua" if sys.platform == "darwin" else "clam")
    except tk.TclError:
        pass
    App(root, url=url, autostart=autostart)
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()


if __name__ == "__main__":
    main()
