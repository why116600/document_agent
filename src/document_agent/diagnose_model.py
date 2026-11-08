"""用最小请求区分服务配置、普通对话参数和工具调用兼容性，不读取业务文件。"""
from __future__ import annotations

import argparse
import os
import json
from urllib.parse import urlsplit, urlunsplit

import httpx
from openai import OpenAI
from pydantic import BaseModel, Field

from document_agent.agent.llm_client import (
    DEFAULT_BASE_URL, DEFAULT_MODEL, describe_model_error, get_deepseek_llm, validate_api_key,
)


class DiagnosticReply(BaseModel):
    ok: bool = Field(description="请求正常处理时返回 true")


def safe_endpoint(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "<已隐藏>" if parts.query else "", ""))


def show_network_environment():
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        value = os.getenv(name) or os.getenv(name.lower())
        detail = "未设置"
        if value:
            detail = "已设置"
            if name != "NO_PROXY":
                try:
                    parsed = urlsplit(value if "://" in value else "http://" + value)
                    detail += f"（{parsed.hostname}:{parsed.port or '默认端口'}；不显示认证信息）"
                except ValueError:
                    pass
        print(f"{name}={detail}")


def network_probe(endpoint, model, key, *, direct=False):
    """独立于SDK测试原始HTTP；只使用现有配置，不改变代理或TLS验证设置。"""
    show_network_environment()
    print("连接模式：" + ("本次请求不读取环境代理（trust_env=False）" if direct else "沿用环境代理（trust_env=True）"))
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(trust_env=not direct, timeout=30) as client:
        print("[网络1/2] GET /models：检查鉴权与模型列表")
        try:
            response = client.get(endpoint.rstrip("/") + "/models", headers=headers)
            response.raise_for_status()
            body = response.json()
            names = [item.get("id") for item in body.get("data", []) if isinstance(item, dict)]
            print("模型列表请求通过；可见模型：" + json.dumps(names[:20], ensure_ascii=False))
            if names and model not in names:
                print("当前 MODEL_NAME 不在返回列表中，请核对模型名称。")
        except Exception as exc:
            print(describe_model_error(exc))
            print("模型列表请求未通过；继续单独检查对话接口，避免将模型列表权限与对话权限混同。")
        print("[网络2/2] 原始 HTTP POST /chat/completions：不经过 OpenAI SDK")
        try:
            response = client.post(endpoint.rstrip("/") + "/chat/completions", headers=headers,
                                   json={"model": model, "messages": [{"role": "user", "content": "Reply OK only."}]})
            response.raise_for_status()
            body = response.json()
            choices = body.get("choices", [])
            if not choices or not choices[0].get("message", {}).get("content"):
                print("HTTP成功但没有取得标准对话文本，尚不能判定接口可用。")
                return 1
            print("原始HTTP对话通过；接下来运行不带 --network 的诊断检查SDK和结构化调用。")
            return 0
        except Exception as exc:
            print(describe_model_error(exc))
            print("原始HTTP请求仍失败；请保留HTTP状态和响应标识，不要发送密钥。")
            return 1


def auth_probe(endpoint, key, *, direct=False):
    """只请求模型列表，区分网络基线与真实 Authorization 头的影响。"""
    show_network_environment()
    print(f"密钥长度：{len(key)} 个字符；以 sk- 开头：{key.startswith('sk-')}；"
          f"全部为可打印ASCII且不含空白：{all(33 <= ord(char) <= 126 for char in key)}")
    print("不显示密钥内容；三个请求只查询 /models，不生成文档或调用模型。")
    cases = [("无密钥", {}),
             ("明确无效的测试字符串", {"Authorization": "Bearer sk-00000000000000000000000000000000"}),
             ("当前窗口的实际密钥", {"Authorization": f"Bearer {key}"})]
    statuses = []
    with httpx.Client(trust_env=not direct, timeout=30) as client:
        for index, (label, headers) in enumerate(cases, 1):
            print(f"[鉴权{index}/3] {label}")
            try:
                response = client.get(endpoint.rstrip("/") + "/models", headers=headers)
                statuses.append(response.status_code)
                response.raise_for_status()
                print(f"HTTP {response.status_code}：请求通过，不打印响应中的模型或账户信息。")
            except Exception as exc:
                if len(statuses) < index:
                    statuses.append(None)
                print(describe_model_error(exc))
    if statuses[0] == 401 and statuses[1] == 401 and statuses[2] == 400:
        print("对照结果：401/401/400。错误与当前密钥加入Authorization头有关，"
              "重点检查复制的密钥内容、长度及该密钥对应的服务端处理；尚不能仅凭400确定具体原因。")
    elif statuses[2] == 200:
        print("实际密钥的模型列表请求已通过；下一步运行普通诊断检查对话接口。")
    elif statuses[2] == 401:
        print("实际密钥收到401鉴权拒绝。请核对官方平台中的有效API密钥。")
    else:
        print("请保留三个HTTP状态及响应标识继续定位，不需要提供密钥内容。")
    return 0 if statuses[2] == 200 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true", help="仅显示当前进程配置，不联网")
    parser.add_argument("--network", action="store_true", help="检查环境代理、模型列表和原始HTTP对话")
    parser.add_argument("--auth-check", action="store_true", help="用无密钥/测试字符串/实际密钥对照模型列表请求，不调用模型")
    parser.add_argument("--direct", action="store_true", help="用于 --network 或 --auth-check：本次诊断不读取环境代理")
    args = parser.parse_args(argv)
    endpoint = (os.getenv("MODEL_BASE_URL") or DEFAULT_BASE_URL).strip()
    model = (os.getenv("MODEL_NAME") or DEFAULT_MODEL).strip()
    key = os.getenv("MODEL_API_KEY", "")
    print(f"MODEL_BASE_URL={safe_endpoint(endpoint)}")
    print(f"MODEL_NAME={model}")
    print("MODEL_API_KEY=" + ("已设置（不显示内容）" if key else "未设置"))
    if args.config_only:
        return 0
    if not key:
        print("请在运行本命令的同一 PowerShell 窗口设置 MODEL_API_KEY。")
        return 1
    try:
        validate_api_key(key)
    except ValueError as exc:
        print(str(exc))
        return 1
    if args.network and args.auth_check:
        parser.error("--network 与 --auth-check 请分别运行")
    if args.direct and not (args.network or args.auth_check):
        parser.error("--direct 需与 --network 或 --auth-check 一起使用")
    if args.auth_check:
        try:
            return auth_probe(endpoint, key, direct=args.direct)
        except Exception as exc:
            print(describe_model_error(exc))
            return 1
    if args.network:
        try:
            return network_probe(endpoint, model, key, direct=args.direct)
        except Exception as exc:
            print(describe_model_error(exc))
            return 1

    print("[1/3] 最小对话请求：仅 model 和 messages，不带 temperature、tools 或业务资料")
    try:
        with OpenAI(api_key=key, base_url=endpoint, timeout=30, max_retries=0) as client:
            response = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": "只回复 OK"}],
            )
        if not response.choices or not response.choices[0].message.content:
            print("未取得文本回复；该模型/端点可能不兼容当前对话接口。")
            return 1
        print("最小请求通过。")
    except Exception as exc:
        print(describe_model_error(exc))
        print("基础请求尚未通过，先核对服务地址、模型名和密钥是否属于同一平台，再根据响应正文修正。")
        return 1

    print("[2/3] 项目普通对话请求（包含当前客户端参数）")
    try:
        llm = get_deepseek_llm()
        response = llm.invoke("只回复 OK")
        if not getattr(response, "content", None):
            print("普通对话没有返回文本。")
            return 2
        print("项目普通对话通过。")
    except Exception as exc:
        print(describe_model_error(exc))
        print("最小请求通过但项目请求失败：重点检查当前客户端附加参数及服务兼容性。")
        return 2

    print("[3/3] 项目结构化输出请求（function_calling）")
    try:
        result = llm.with_structured_output(
            DiagnosticReply, method="function_calling", include_raw=True,
        ).invoke("这是接口自检，请返回 ok=true。")
        parsed = result.get("parsed") if isinstance(result, dict) else None
        if not isinstance(parsed, DiagnosticReply) or not parsed.ok:
            print("未获得合法结构化结果；普通对话可用，工具调用/结构化输出仍需检查。")
            return 3
        print("三项检查通过，可重新运行 complete 场景。")
        return 0
    except Exception as exc:
        print(describe_model_error(exc))
        print("普通对话通过但结构化调用失败：重点检查所选模型/服务是否支持工具调用及对应参数。")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
