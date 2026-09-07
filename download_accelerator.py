#!/usr/bin/env python3
"""
Acelerador de download: abre varias conexoes HTTP em paralelo (Range requests),
cada uma baixa um pedaco do arquivo e escreve direto na posicao certa do
arquivo final. Sem dependencias externas, so a biblioteca padrao.

Recursos:
  - numero de conexoes ajustavel durante o download (Download.set_connections)
  - retomada: o progresso de cada pedaco fica em <arquivo>.part.json; se o
    download parar (cancelamento, app fechado, queda de energia), rodar de novo
    com o mesmo destino continua de onde parou

Uso (linha de comando):
    python3 download_accelerator.py URL [-o SAIDA] [-n CONEXOES] [--chunk MiB] [--sha256 HASH] [--no-resume]

Exemplo (Ubuntu 26.04.1 desktop, 6,5 GB):
    python3 download_accelerator.py \
        https://releases.ubuntu.com/26.04.1/ubuntu-26.04.1-desktop-amd64.iso \
        -n 8 --sha256 601e30fbf5d97759367c632e2c33630665039b7e2158fd068403da3ccf1bda1f

Este mesmo arquivo e usado como motor pela interface grafica
(download_accelerator_gui.py), atraves da classe Download.
"""

import argparse
import hashlib
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "download-accelerator/2.0"
READ_BUFFER = 256 * 1024      # 256 KiB por leitura de socket
STATE_SAVE_INTERVAL = 1.0     # segundos entre gravacoes do arquivo de estado
STATE_VERSION = 1

# Estados de cada pedaco (usados pelo mapa da interface)
PENDING, ACTIVE, DONE, FAILED = 0, 1, 2, 3


class DownloadError(Exception):
    pass


class DownloadCancelled(Exception):
    def __init__(self, part_path):
        super().__init__("download cancelado")
        self.part_path = part_path


# --------------------------------------------------------------------------- #
# Progresso (compartilhado entre as threads; quem exibe e o CLI ou a GUI)
# --------------------------------------------------------------------------- #
class Progress:
    def __init__(self, total, n_chunks=0):
        self.total = total
        self.done = 0
        self.session_done = 0          # bytes baixados nesta execucao (para a velocidade)
        self.chunk_done = [0] * n_chunks
        self.chunk_state = [PENDING] * n_chunks
        self.started_at = time.monotonic()
        self._lock = threading.Lock()

    def add(self, n, idx=None):
        with self._lock:
            self.done += n
            self.session_done += n
            if idx is not None:
                self.chunk_done[idx] += n

    def set_state(self, idx, state):
        with self._lock:
            self.chunk_state[idx] = state

    def snapshot(self):
        """Copia consistente para quem for desenhar ou salvar."""
        with self._lock:
            return self.done, list(self.chunk_done), list(self.chunk_state)

    @property
    def elapsed(self):
        return time.monotonic() - self.started_at


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024


def fmt_time(s):
    if s is None or s == float("inf") or s != s:
        return "--:--"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def open_url(url, headers, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    return urllib.request.urlopen(req, timeout=timeout)


def probe(url, timeout):
    """Descobre tamanho, suporte a Range, URL final e identificadores do arquivo."""
    with open_url(url, {"Range": "bytes=0-0"}, timeout) as r:
        ident = {"etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified")}
        if r.status == 206:
            content_range = r.headers.get("Content-Range", "")  # "bytes 0-0/12345"
            total_str = content_range.rsplit("/", 1)[-1]
            total = int(total_str) if total_str.isdigit() else None
            return {"total": total, "ranges": True, "url": r.geturl(), **ident}
        length = r.headers.get("Content-Length")
        total = int(length) if length and length.isdigit() else None
        return {"total": total, "ranges": False, "url": r.geturl(), **ident}


# Escrita posicional thread-safe. os.pwrite nao existe no Windows, entao
# cai num seek+write protegido por lock.
if hasattr(os, "pwrite"):
    def write_at(fd, data, pos):
        os.pwrite(fd, data, pos)
else:
    _write_lock = threading.Lock()

    def write_at(fd, data, pos):
        with _write_lock:
            os.lseek(fd, pos, os.SEEK_SET)
            os.write(fd, data)


def fetch_range(url, idx, start, end, already, fd, progress, timeout, retries, cancel,
                should_yield=None):
    """
    Baixa bytes [start + already, end] e grava no fd. Em erro, retoma de onde
    parou. `already` e o que ja estava no disco (retomada).

    Devolve True se o pedaco terminou, False se parou antes (cancelamento ou
    should_yield() pediu para largar o pedaco; o que ja foi gravado fica
    contado em progress.chunk_done e pode ser retomado).
    """
    if cancel.is_set():
        return False
    progress.set_state(idx, ACTIVE)
    pos = start + already
    last_err = None
    for attempt in range(1, retries + 1):
        if cancel.is_set():
            progress.set_state(idx, PENDING)
            return False
        try:
            with open_url(url, {"Range": f"bytes={pos}-{end}"}, timeout) as r:
                if r.status != 206:
                    raise IOError(f"servidor respondeu {r.status} em vez de 206 (ignorou o Range)")
                while pos <= end:
                    if cancel.is_set() or (should_yield and should_yield()):
                        progress.set_state(idx, PENDING)
                        return False
                    buf = r.read(min(READ_BUFFER, end - pos + 1))
                    if not buf:
                        break
                    write_at(fd, buf, pos)
                    pos += len(buf)
                    progress.add(len(buf), idx)
            if pos == end + 1:
                progress.set_state(idx, DONE)
                return True
            raise IOError(f"segmento incompleto: {pos - start}/{end - start + 1} bytes")
        except Exception as e:  # noqa: BLE001 - qualquer falha de rede vale retry
            last_err = e
            if attempt < retries:
                time.sleep(min(2 ** attempt, 15))
    progress.set_state(idx, FAILED)
    raise DownloadError(f"pedaco {idx} ({start}-{end}) falhou apos {retries} tentativas: {last_err}")


def download_single(url, fd, progress, timeout, cancel):
    """Fallback quando o servidor nao aceita Range: uma conexao so, sem retomada."""
    with open_url(url, {}, timeout) as r:
        pos = 0
        while not cancel.is_set():
            buf = r.read(READ_BUFFER)
            if not buf:
                break
            write_at(fd, buf, pos)
            pos += len(buf)
            progress.add(len(buf))
    return pos


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def build_chunks(total, chunk_size):
    return [(s, min(s + chunk_size, total) - 1) for s in range(0, total, chunk_size)]


def sha256_of(path, on_progress=None):
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
            read += len(block)
            if on_progress:
                on_progress(read)
    return h.hexdigest()


def resolve_output(url, output):
    """output pode ser None (nome da URL no cwd), uma pasta, ou um caminho de arquivo."""
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path)) or "download.bin"
    if not output:
        return name
    if os.path.isdir(output):
        return os.path.join(output, name)
    return output


def state_path_for(part_path):
    return part_path + ".json"


def load_state(part_path):
    """Le o arquivo de estado de um .part; None se nao existir ou for invalido."""
    try:
        with open(state_path_for(part_path), "r", encoding="utf-8") as f:
            st = json.load(f)
        if st.get("version") != STATE_VERSION or not isinstance(st.get("chunk_done"), list):
            return None
        return st
    except (OSError, ValueError):
        return None


def discard_partial(out_path):
    """Apaga o .part e o estado de um download interrompido."""
    part = out_path + ".part"
    for p in (part, state_path_for(part)):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# Download com pool dinamico de conexoes e retomada
# --------------------------------------------------------------------------- #
class Download:
    """
    d = Download(url, pasta_ou_arquivo, connections=8)
    path = d.run()            # bloqueia; levante DownloadCancelled / DownloadError
    d.set_connections(12)     # de outra thread, a qualquer momento
    d.cancel.set()            # cancela; o progresso fica salvo para retomar

    on_start(progress, info) e chamado assim que o servidor foi consultado.
    """

    def __init__(self, url, output=None, connections=8, chunk_mib=16, timeout=30,
                 retries=5, resume=True, cancel=None, on_start=None):
        self.url = url
        self.output_arg = output
        self.chunk_size = max(int(chunk_mib * 1024 * 1024), 1024 * 1024)
        self.timeout = timeout
        self.retries = retries
        self.resume = resume
        self.cancel = cancel if cancel is not None else threading.Event()
        self.on_start = on_start

        self.progress = None
        self.info = None
        self._target = max(1, int(connections))
        self._lock = threading.Lock()
        self._tasks = queue.Queue()
        self._workers = []
        self._active = 0
        self._error = None
        self._fd = None
        self._chunks = []
        self._running = False

    # ---- controle externo -------------------------------------------------
    def set_connections(self, n):
        with self._lock:
            self._target = max(1, int(n))
            if self._running:
                self._spawn_locked()

    @property
    def connections(self):
        """Quantas conexoes estao ativas agora."""
        with self._lock:
            return self._active

    @property
    def target_connections(self):
        with self._lock:
            return self._target

    # ---- pool dinamico ----------------------------------------------------
    def _spawn_locked(self):
        while self._active < self._target and self._active < self._tasks.qsize() + self._active_transfers():
            t = threading.Thread(target=self._worker, daemon=True)
            self._active += 1
            self._workers.append(t)
            t.start()

    def _active_transfers(self):
        # numero de pedacos ainda nao concluidos que estao com alguma thread
        return 0 if self.progress is None else sum(1 for s in self.progress.chunk_state if s == ACTIVE)

    def _excess(self):
        # sem lock de proposito: e so uma dica lida no meio da transferencia
        return self._active > self._target

    def _worker(self):
        try:
            while not self.cancel.is_set():
                with self._lock:
                    if self._active > self._target:
                        return  # conexoes reduzidas: esta sobra encerra
                try:
                    idx = self._tasks.get_nowait()
                except queue.Empty:
                    return
                start, end = self._chunks[idx]
                already = self.progress.chunk_done[idx]
                try:
                    finished = fetch_range(self.info["url"], idx, start, end, already, self._fd,
                                           self.progress, self.timeout, self.retries, self.cancel,
                                           should_yield=self._excess)
                except Exception as e:  # noqa: BLE001
                    with self._lock:
                        if self._error is None:
                            self._error = e
                    self.cancel.set()
                    return
                if not finished:
                    # largou o pedaco no meio (reducao de conexoes ou cancelamento):
                    # volta para a fila e continua de onde parou quando alguem pegar
                    self._tasks.put(idx)
                    if self._excess():
                        return
        finally:
            with self._lock:
                self._active -= 1

    # ---- estado em disco --------------------------------------------------
    def _save_state(self):
        if self.progress is None or not self.info.get("parallel"):
            return
        done, chunk_done, _ = self.progress.snapshot()
        st = {
            "version": STATE_VERSION,
            "url": self.url, "final_url": self.info["url"],
            "total": self.info["total"], "chunk_size": self.info["chunk_size"],
            "etag": self.info.get("etag"), "last_modified": self.info.get("last_modified"),
            "chunk_done": chunk_done, "done": done, "updated": time.time(),
        }
        tmp = self.info["state"] + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f)
            os.replace(tmp, self.info["state"])
        except OSError:
            pass

    def _try_resume(self, part, meta):
        """Devolve chunk_done salvo se o .part existente puder ser retomado."""
        if not self.resume or not meta["ranges"] or meta["total"] is None:
            return None
        st = load_state(part)
        if st is None or not os.path.exists(part):
            return None
        if st.get("total") != meta["total"] or os.path.getsize(part) != meta["total"]:
            return None
        for key in ("etag", "last_modified"):
            if st.get(key) and meta.get(key) and st[key] != meta[key]:
                return None  # o arquivo no servidor mudou
        chunks = build_chunks(meta["total"], st["chunk_size"])
        saved = st["chunk_done"]
        if len(saved) != len(chunks):
            return None
        for (s, e), d in zip(chunks, saved):
            if not isinstance(d, int) or d < 0 or d > e - s + 1:
                return None
        return st["chunk_size"], saved

    # ---- execucao ---------------------------------------------------------
    def run(self):
        try:
            meta = probe(self.url, self.timeout)
        except urllib.error.HTTPError as e:
            raise DownloadError(f"Erro HTTP {e.code}: {e.reason}") from e
        except Exception as e:  # noqa: BLE001
            raise DownloadError(f"Nao consegui acessar a URL: {e}") from e

        out = resolve_output(meta["url"], self.output_arg)
        part = out + ".part"
        total = meta["total"]

        resumed = self._try_resume(part, meta)
        chunk_size = resumed[0] if resumed else self.chunk_size
        parallel = meta["ranges"] and total is not None and total > chunk_size
        self._chunks = build_chunks(total, chunk_size) if parallel else []
        self.progress = Progress(total, len(self._chunks))

        already = 0
        if resumed:
            _, saved = resumed
            for i, ((s, e), d) in enumerate(zip(self._chunks, saved)):
                self.progress.chunk_done[i] = d
                if d == e - s + 1:
                    self.progress.chunk_state[i] = DONE
            already = sum(saved)
            self.progress.done = already

        self.info = {
            "url": meta["url"], "output": out, "part": part, "state": state_path_for(part),
            "total": total, "parallel": parallel, "ranges_ok": meta["ranges"],
            "chunks": len(self._chunks), "chunk_size": chunk_size,
            "etag": meta.get("etag"), "last_modified": meta.get("last_modified"),
            "resumed": bool(resumed), "resumed_bytes": already,
            "download": self,
        }
        if self.on_start:
            self.on_start(self.progress, self.info)

        flags = os.O_RDWR | os.O_CREAT | (0 if resumed else os.O_TRUNC)
        self._fd = os.open(part, flags, 0o644)
        try:
            if parallel:
                if not resumed:
                    os.ftruncate(self._fd, total)  # pre-aloca o arquivo inteiro
                written = self._run_parallel()
            else:
                written = download_single(meta["url"], self._fd, self.progress, self.timeout, self.cancel)
        finally:
            os.close(self._fd)
            self._fd = None

        if self._error is not None:
            self._save_state()
            raise DownloadError(str(self._error))
        if self.cancel.is_set():
            self._save_state()
            raise DownloadCancelled(part)
        if total is not None and written != total:
            self._save_state()
            raise DownloadError(f"Tamanho final {written} difere do esperado {total}. Parcial em: {part}")

        try:
            os.remove(self.info["state"])
        except FileNotFoundError:
            pass
        os.replace(part, out)
        return out

    def _run_parallel(self):
        for i, s in enumerate(self.progress.chunk_state):
            if s != DONE:
                self._tasks.put(i)
        with self._lock:
            self._running = True
            self._spawn_locked()

        last_save = time.monotonic()
        try:
            while True:
                alive = [w for w in self._workers if w.is_alive()]
                if not alive:
                    # sem threads vivas: acabou, cancelou, deu erro, ou sobraram
                    # tarefas depois de uma reducao seguida de aumento
                    if self.cancel.is_set() or self._tasks.empty():
                        break
                    with self._lock:
                        self._spawn_locked()
                        if not self._workers or not any(w.is_alive() for w in self._workers):
                            break
                time.sleep(0.25)
                with self._lock:
                    self._spawn_locked()  # repoe conexoes se sobrou tarefa e falta gente
                if time.monotonic() - last_save >= STATE_SAVE_INTERVAL:
                    self._save_state()
                    last_save = time.monotonic()
        except BaseException:
            # Ctrl+C ou similar: para as conexoes antes de fechar o arquivo,
            # senao o estado salvo pode ficar a frente do que esta no disco.
            self.cancel.set()
            for w in self._workers:
                w.join(timeout=5)
            self._save_state()
            raise
        finally:
            with self._lock:
                self._running = False

        for w in self._workers:
            w.join(timeout=5)
        done, _, _ = self.progress.snapshot()
        return done


def run_download(url, output=None, connections=8, chunk_mib=16, timeout=30,
                 retries=5, cancel=None, on_start=None, resume=True):
    """Atalho funcional; devolve o caminho final do arquivo."""
    return Download(url, output, connections, chunk_mib, timeout, retries,
                    resume, cancel, on_start).run()


# --------------------------------------------------------------------------- #
# Interface de linha de comando
# --------------------------------------------------------------------------- #
class ConsoleReporter(threading.Thread):
    """Imprime a barra de progresso no terminal a cada 0,5 s."""

    def __init__(self, progress, download=None):
        super().__init__(daemon=True)
        self.p = progress
        self.d = download
        self._stop = threading.Event()

    def run(self):
        last_t, last_b = time.monotonic(), 0
        while not self._stop.wait(0.5):
            now = time.monotonic()
            done, _, _ = self.p.snapshot()
            speed = (done - last_b) / max(now - last_t, 1e-6)
            last_t, last_b = now, done
            self._print(done, speed)

    def stop(self):
        self._stop.set()
        self.join(timeout=2)
        done, _, _ = self.p.snapshot()
        self._print(done, self.p.session_done / max(self.p.elapsed, 1e-6))
        sys.stdout.write(f"\nTempo: {fmt_time(self.p.elapsed)}\n")
        sys.stdout.flush()

    def _print(self, done, speed):
        total = self.p.total
        conns = f"  {self.d.connections} conx" if self.d else ""
        if total:
            pct = done / total * 100
            eta = (total - done) / speed if speed > 0 else None
            filled = int(30 * done / total)
            bar = "#" * filled + "-" * (30 - filled)
            line = (f"\r[{bar}] {pct:5.1f}%  {fmt_bytes(done)}/{fmt_bytes(total)}"
                    f"  {fmt_bytes(speed)}/s{conns}  ETA {fmt_time(eta)}   ")
        else:
            line = f"\r{fmt_bytes(done)}  {fmt_bytes(speed)}/s   "
        sys.stdout.write(line)
        sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description="Download com multiplas conexoes paralelas e retomada.")
    ap.add_argument("url", help="URL do arquivo")
    ap.add_argument("-o", "--output", help="arquivo ou pasta de saida (padrao: nome da URL na pasta atual)")
    ap.add_argument("-n", "--connections", type=int, default=8, help="conexoes simultaneas (padrao 8)")
    ap.add_argument("--chunk", type=float, default=16, help="tamanho de cada pedaco em MiB (padrao 16)")
    ap.add_argument("--timeout", type=float, default=30, help="timeout por conexao em segundos")
    ap.add_argument("--retries", type=int, default=5, help="tentativas por pedaco")
    ap.add_argument("--sha256", help="hash esperado; verifica ao final")
    ap.add_argument("--no-resume", action="store_true", help="ignora um .part existente e recomeca do zero")
    args = ap.parse_args()
    if args.connections < 1:
        ap.error("-n precisa ser >= 1")

    reporter = None

    def on_start(progress, info):
        nonlocal reporter
        print(f"Arquivo : {info['output']}")
        print(f"Tamanho : {fmt_bytes(info['total']) if info['total'] else 'desconhecido'}")
        if info["parallel"]:
            print(f"Modo    : {args.connections} conexoes, {info['chunks']} pedacos "
                  f"de {fmt_bytes(info['chunk_size'])}")
        else:
            why = "servidor nao aceita Range" if not info["ranges_ok"] else "arquivo pequeno ou tamanho desconhecido"
            print(f"Modo    : 1 conexao ({why})")
        if info["resumed"]:
            print(f"Retomando: {fmt_bytes(info['resumed_bytes'])} ja estavam baixados")
        reporter = ConsoleReporter(progress, info["download"])
        reporter.start()

    print(f"Consultando {args.url}")
    d = Download(args.url, args.output, args.connections, args.chunk, args.timeout,
                 args.retries, resume=not args.no_resume, on_start=on_start)

    def on_term(_signum, _frame):  # kill/SIGTERM salva o progresso como o Ctrl+C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    try:
        out = d.run()
    except KeyboardInterrupt:
        d.cancel.set()
        d._save_state()
        if reporter:
            reporter.stop()
        sys.exit("\nInterrompido. Rode o mesmo comando de novo para retomar de onde parou.")
    except DownloadCancelled as e:
        if reporter:
            reporter.stop()
        sys.exit(f"\nCancelado. Rode de novo para retomar; parcial em: {e.part_path}")
    except DownloadError as e:
        if reporter:
            reporter.stop()
        sys.exit(f"\nFalha: {e}")
    if reporter:
        reporter.stop()

    if args.sha256:
        print("Verificando SHA-256...")
        got = sha256_of(out)
        if got.lower() == args.sha256.lower():
            print("SHA-256 confere.")
        else:
            print(f"SHA-256 NAO confere!\n  esperado: {args.sha256}\n  obtido  : {got}")
            sys.exit(2)

    print(f"Salvo em: {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
