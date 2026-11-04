#!/usr/bin/env bash
# =====================================================================
# 文档智能体 Web 服务管理脚本（Linux / macOS）
#
# 用法：
#   ./start.sh start            启动服务（默认动作）
#   ./start.sh stop             停止服务
#   ./start.sh restart          重启服务
#   ./start.sh status           查看运行状态
#   ./start.sh help             查看帮助
#
# 可选参数：
#   --port <端口>               临时覆盖 src/setting.json 中的 web_port
#   --host <监听地址>           临时覆盖监听地址（默认按 setting.json 的 web_allow_remote）
#   --no-browser                启动后不自动打开浏览器
#
# 监听地址优先级：命令行 --host > src/setting.json 的 web_allow_remote：
#   "web_allow_remote": false   仅本机访问（监听 127.0.0.1，默认）
#   "web_allow_remote": true    允许远程访问（监听 0.0.0.0）
#   "web_port": 8000            默认端口
#
# 运行状态保存在项目根目录的 .run 目录（server.pid / server.log）。
# 检测到桌面环境（DISPLAY / WAYLAND_DISPLAY）时，启动成功会自动打开前端页面。
# =====================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
SRC_DIR="$PROJECT_ROOT/src"
RUN_DIR="$PROJECT_ROOT/.run"
PID_FILE="$RUN_DIR/server.pid"
LOG_FILE="$RUN_DIR/server.log"

ACTION=""
PORT_OVERRIDE=""
HOST_OVERRIDE=""
BIND_HOST=""
PORT=""
URL=""
NO_BROWSER="${DOC_AGENT_NO_BROWSER:-0}"

print_usage() {
    cat <<'EOF'
文档智能体 Web 服务管理脚本（Linux / macOS）

用法：
  ./start.sh start              启动服务（默认）
  ./start.sh stop               停止服务
  ./start.sh restart            重启服务
  ./start.sh status             查看运行状态

可选参数：
  --port <端口>                 临时覆盖 src/setting.json 中的 web_port
  --host <监听地址>             临时覆盖监听地址（例如 0.0.0.0；优先级高于配置）
  --no-browser                  启动后不自动打开浏览器

监听地址默认由 src/setting.json 决定（修改后需重启）：
  "web_allow_remote": false     仅本机访问（监听 127.0.0.1，默认）
  "web_allow_remote": true      允许远程访问（监听 0.0.0.0）
  "web_port": 8000              默认端口

首次运行前请设置大模型密钥：
  export MODEL_API_KEY="你的密钥"
EOF
}

log_info() { printf '\033[36m[文档智能体]\033[0m %s\n' "$*"; }
log_warn() { printf '\033[33m[文档智能体]\033[0m %s\n' "$*" >&2; }

# ------------------------------------------------------------------ 参数解析
while [[ $# -gt 0 ]]; do
    case "$1" in
        start|stop|restart|status)
            ACTION="$1"; shift ;;
        help|-h|--help)
            ACTION="help"; shift ;;
        --port)
            [[ $# -ge 2 ]] || { log_warn "--port 需要一个端口号"; exit 2; }
            if [[ ! "$2" =~ ^[0-9]+$ ]] || (( 10#$2 < 1 || 10#$2 > 65535 )); then
                log_warn "端口不合法：$2"
                exit 2
            fi
            PORT_OVERRIDE="$2"; shift 2 ;;
        --host)
            [[ $# -ge 2 ]] || { log_warn "--host 需要一个监听地址"; exit 2; }
            HOST_OVERRIDE="$2"; shift 2 ;;
        --no-browser)
            NO_BROWSER=1; shift ;;
        *)
            log_warn "未知参数：$1"
            print_usage
            exit 2 ;;
    esac
done

[[ -n "$ACTION" ]] || ACTION="start"

# ------------------------------------------------------------------ Python 环境
python_usable() {
    local candidate="$1"
    [[ -n "$candidate" ]] || return 1
    # 有些系统上的 python3 只是应用商店占位符，必须实际执行一次再确认
    "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

resolve_python() {
    if [[ -n "${DOC_AGENT_PYTHON:-}" ]]; then
        if [[ ! -x "$DOC_AGENT_PYTHON" ]] || ! python_usable "$DOC_AGENT_PYTHON"; then
            log_warn "环境变量 DOC_AGENT_PYTHON 不是可用的 Python 3.10+ 解释器：$DOC_AGENT_PYTHON"
            exit 1
        fi
        printf '%s\n' "$DOC_AGENT_PYTHON"
        return
    fi
    local candidate path
    for candidate in python3 python; do
        path="$(command -v "$candidate" 2>/dev/null || true)"
        if [[ -n "$path" ]] && python_usable "$path"; then
            printf '%s\n' "$path"
            return
        fi
    done
    log_warn "未找到可用的 Python 3.10+（已尝试 python3、python），请安装或设置 DOC_AGENT_PYTHON"
    exit 1
}

check_dependencies() {
    local python="$1"
    local missing
    missing="$("$python" -c 'import importlib.util as u; print(",".join(m for m in ("fastapi", "uvicorn", "multipart") if u.find_spec(m) is None))' 2>/dev/null || true)"
    if [[ -n "$missing" ]]; then
        log_warn "缺少 Python 依赖：${missing}"
        log_warn "请先执行：\"$python\" -m pip install -r \"$PROJECT_ROOT/requirements.txt\""
        exit 1
    fi
}

port_busy() {
    local python="$1"
    "$python" - "$(probe_host)" "$PORT" <<'PY' 2>/dev/null || return 1
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(1)
try:
    sock.connect((host, port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

fetch_health() {
    local python="$1"
    PYTHONIOENCODING=utf-8 "$python" - "$URL" <<'PY' 2>/dev/null
import json
import sys
import urllib.request

url = sys.argv[1].rstrip("/") + "/api/health"
try:
    with urllib.request.urlopen(url, timeout=3) as response:
        data = json.loads(response.read().decode("utf-8"))
except Exception:
    sys.exit(1)
print("llm" if data.get("llm_configured") else "no-llm")
print((data.get("knowledge") or {}).get("message") or "")
print("yes" if data.get("remote_access") else "no")
print(data.get("bind_host") or "")
print(data.get("host_source") or "config")
for item in data.get("remote_urls") or []:
    print(item)
PY
}

health_ok() {
    local python="$1"
    fetch_health "$python" >/dev/null 2>&1
}

show_remote_access() {
    local health="$1"
    local flag bind_host host_source line label
    flag="$(printf '%s\n' "$health" | sed -n '3p')"
    bind_host="$(printf '%s\n' "$health" | sed -n '4p')"
    host_source="$(printf '%s\n' "$health" | sed -n '5p')"
    if [[ "$host_source" == "command-line" ]]; then
        label="命令行 --host 覆盖"
    else
        label="setting.json 配置"
    fi
    if [[ "$flag" == "yes" ]]; then
        log_info "远程访问：已允许（监听 ${bind_host}:${PORT}，来源：${label}）"
        while IFS= read -r line; do
            [[ -n "$line" ]] && log_info "  其他主机访问：$line"
        done <<< "$(printf '%s\n' "$health" | tail -n +6)"
    else
        log_info "远程访问：未允许（仅本机，来源：${label}）"
    fi
}

wait_health() {
    local python="$1"
    local timeout="${2:-90}"
    local waited=0
    while (( waited < timeout )); do
        if health_ok "$python"; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

open_browser() {
    if [[ "$NO_BROWSER" == "1" ]]; then
        return 0
    fi
    if [[ "$(uname -s)" == "Darwin" ]]; then
        if command -v open >/dev/null 2>&1 && open "$URL" >/dev/null 2>&1; then
            log_info "已在默认浏览器打开：$URL"
            return 0
        fi
    elif [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
        if command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1; then
            log_info "已在默认浏览器打开：$URL"
            return 0
        fi
    fi
    log_info "未检测到可用的桌面环境，请手动访问：$URL"
}

# ------------------------------------------------------------------ 监听配置
load_web_config() {
    local python endpoint
    python="$(resolve_python)"
    local extra=()
    [[ -n "$PORT_OVERRIDE" ]] && extra+=(--port "$PORT_OVERRIDE")
    [[ -n "$HOST_OVERRIDE" ]] && extra+=(--host "$HOST_OVERRIDE")

    endpoint="$(cd "$SRC_DIR" && "$python" -m document_agent.backend --print-endpoint "${extra[@]}" 2>/dev/null || true)"
    IFS='|' read -r BIND_HOST PORT URL <<< "$endpoint"

    if [[ -z "${BIND_HOST:-}" || -z "${PORT:-}" || -z "${URL:-}" ]]; then
        log_warn "读取 setting.json 监听配置失败，按默认配置处理"
        BIND_HOST="${HOST_OVERRIDE:-127.0.0.1}"
        PORT="${PORT_OVERRIDE:-8000}"
        if [[ "$BIND_HOST" == "0.0.0.0" || "$BIND_HOST" == "::" ]]; then
            URL="http://127.0.0.1:${PORT}/"
        else
            URL="http://${BIND_HOST}:${PORT}/"
        fi
    fi
}

probe_host() {
    if [[ "$BIND_HOST" == "0.0.0.0" || "$BIND_HOST" == "::" || -z "$BIND_HOST" ]]; then
        printf '127.0.0.1'
    else
        printf '%s' "$BIND_HOST"
    fi
}

# 监听地址优先级：命令行 --host > setting.json 的 web_allow_remote
# help 不需要读取配置，避免没装 Python 时连帮助都看不了
if [[ "$ACTION" != "help" ]]; then
    load_web_config
fi

# ------------------------------------------------------------------ 动作
start_server() {
    local python
    python="$(resolve_python)"

    if [[ -f "$PID_FILE" ]]; then
        local existing
        existing="$(cat "$PID_FILE" 2>/dev/null || true)"
        if [[ -n "$existing" ]] && kill -0 "$existing" 2>/dev/null; then
            log_info "服务已在运行（PID $existing）：$URL"
            open_browser
            return 0
        fi
        rm -f "$PID_FILE"
    fi

    if port_busy "$python"; then
        log_warn "端口 $PORT 已被其它程序占用，请用 --port 指定其它端口"
        exit 1
    fi
    check_dependencies "$python"

    mkdir -p "$RUN_DIR"
    : > "$LOG_FILE"

    log_info "正在启动服务，日志：$LOG_FILE"
    (
        cd "$SRC_DIR"
        # 命令行 --host 优先；未指定时由 setting.json 的 web_allow_remote 决定
        host_args=()
        [[ -n "$HOST_OVERRIDE" ]] && host_args=(--host "$HOST_OVERRIDE")
        exec nohup env PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 "$python" -m document_agent.backend \
            --port "$PORT" "${host_args[@]}" >> "$LOG_FILE" 2>&1
    ) &
    local server_pid=$!
    printf '%s\n' "$server_pid" > "$PID_FILE"

    if ! wait_health "$python" 90; then
        log_warn "服务未在 90 秒内就绪，启动可能失败。"
        if [[ ! -s "$LOG_FILE" ]]; then
            log_warn "日志为空：服务进程可能没能启动（使用的解释器：$python）"
        else
            log_warn "最近的日志："
            tail -n 40 "$LOG_FILE" >&2 || true
        fi
        log_warn "可执行 $0 stop 清理进程后重试"
        exit 1
    fi

    log_info "启动成功（PID $server_pid）：$URL"
    local health
    health="$(fetch_health "$python" || true)"
    if [[ "$health" == no-llm* ]]; then
        log_warn "未检测到 MODEL_API_KEY，界面可以打开，但生成文档会失败。"
        log_warn "请先执行：export MODEL_API_KEY=\"你的密钥\"，然后 ./start.sh restart"
    fi
    local knowledge_message
    knowledge_message="$(printf '%s\n' "$health" | sed -n '2p')"
    if [[ -n "$knowledge_message" ]]; then
        log_info "知识库：$knowledge_message"
    fi
    show_remote_access "$health"
    open_browser
}

stop_server() {
    if [[ ! -f "$PID_FILE" ]]; then
        log_info "服务未在运行"
        return 0
    fi
    local server_pid
    server_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -z "$server_pid" ]] || ! kill -0 "$server_pid" 2>/dev/null; then
        log_info "服务未在运行"
        rm -f "$PID_FILE"
        return 0
    fi

    log_info "正在停止服务（PID $server_pid）…"
    kill "$server_pid" 2>/dev/null || true
    local waited=0
    while kill -0 "$server_pid" 2>/dev/null && (( waited < 20 )); do
        sleep 0.5
        waited=$((waited + 1))
    done
    if kill -0 "$server_pid" 2>/dev/null; then
        log_warn "进程未在 10 秒内退出，强制结束"
        kill -9 "$server_pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    log_info "服务已停止"
}

status_server() {
    local python
    python="$(resolve_python)"

    local server_pid=""
    if [[ -f "$PID_FILE" ]]; then
        server_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    fi

    if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
        log_info "状态：运行中  PID=$server_pid  端口=$PORT"
        log_info "访问地址：$URL"
        local health
        health="$(fetch_health "$python" || true)"
        if [[ -n "$health" ]]; then
            log_info "健康检查：正常"
            show_remote_access "$health"
        else
            log_warn "健康检查：失败，请查看日志：$LOG_FILE"
        fi
        log_info "日志文件：$LOG_FILE"
        return 0
    fi

    if health_ok "$python"; then
        log_warn "未找到 PID 文件，但 $URL 有服务响应（可能是手动启动的实例）"
        return 0
    fi

    log_info "状态：未运行"
    return 1
}

restart_server() {
    stop_server
    sleep 1
    start_server
}

case "$ACTION" in
    start) start_server ;;
    stop) stop_server ;;
    restart) restart_server ;;
    status) status_server ;;
    help) print_usage ;;
esac
