"""Hysteria2 服务端配置模型：字段元数据、校验、YAML 生成、分享链接。

纯逻辑模块，不依赖 tkinter，可单独用 CLI 或单元测试驱动。
字段定义全部外置在 schema/fields.json，改字段不需要改本文件。
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import yaml

# 打包成 exe 后 __file__ 指向临时解压目录，配置必须落在 exe 同级目录才不会重启丢失
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", str(ROOT)))
else:
    ROOT = Path(__file__).resolve().parent
    RESOURCE_DIR = ROOT

SCHEMA_PATH = RESOURCE_DIR / "schema" / "fields.json"
PROFILES_DIR = ROOT / "profiles"
BIN_DIR = ROOT / "bin"
LOGS_DIR = ROOT / "logs"
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.yaml"

BANDWIDTH_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(bps|b|kbps|kb|k|mbps|mb|m|gbps|gb|g|tbps|tb|t)?\s*$", re.I)
PORT_RANGE_RE = re.compile(r"[-,]")
VALID_OBFS = ("salamander", "gecko")
VALID_OUTBOUND = ("direct", "socks5", "http")
VALID_AUTH = ("password", "userpass", "http", "command")


def ensure_dirs() -> None:
    for d in (PROFILES_DIR, BIN_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_schema() -> dict:
    with SCHEMA_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def fields(schema: dict | None = None) -> list[dict]:
    schema = schema or load_schema()
    return [f for tab in schema["tabs"] for f in tab["fields"] if "key" in f]


def field_map(schema: dict | None = None) -> dict[str, dict]:
    return {f["key"]: f for f in fields(schema)}


def default_values(schema: dict | None = None) -> dict[str, Any]:
    """从 schema 生成一份默认值；密码类字段用 CSPRNG 现场生成，避免固定弱口令。"""
    out: dict[str, Any] = {}
    for f in fields(schema):
        out[f["key"]] = copy.deepcopy(f.get("default"))
    out["auth.password"] = secrets.token_urlsafe(12)
    return out


# ---------- profile 持久化 ----------

def _safe_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name or ""):
        raise ValueError("配置名只允许字母、数字、下划线、连字符，长度 1-64")
    return name


def profile_path(name: str) -> Path:
    return PROFILES_DIR / f"{_safe_name(name)}.json"


def list_profiles() -> list[str]:
    ensure_dirs()
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def load_profile(name: str) -> dict[str, Any]:
    path = profile_path(name)
    if not path.exists():
        # 缺配置时给一份可用默认值，不阻塞启动流程
        return default_values()
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    merged = default_values()
    merged.update(data)
    return merged


def save_profile(name: str, values: dict[str, Any]) -> Path:
    ensure_dirs()
    path = profile_path(name)
    tmp = path.with_suffix(".json.tmp")
    # 原子写：先写临时文件再替换，避免写一半导致配置损坏
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(values, fp, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def delete_profile(name: str) -> None:
    path = profile_path(name)
    if path.exists():
        path.unlink()


# ---------- 通用字典工具 ----------

def set_path(target: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = target
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def get_path(source: dict, path: str, default: Any = None) -> Any:
    cur: Any = source
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def deep_merge(base: dict, extra: dict) -> dict:
    """递归合并，extra 的同名键覆盖 base；用于高级 YAML 覆盖表单值。"""
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def prune(node: Any) -> Any:
    """去掉空 dict / 空 list / None，保留 False 和 0 这类有效标量。"""
    if isinstance(node, dict):
        cleaned = {k: prune(v) for k, v in node.items()}
        return {k: v for k, v in cleaned.items() if v is not None and v != {} and v != [] and v != ""}
    if isinstance(node, list):
        return [prune(v) for v in node]
    return node


# ---------- 值转换 ----------

def parse_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip() and not line.strip().startswith("#")]


def parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in parse_lines(text):
        if ":" not in line and "=" not in line:
            continue
        sep = ":" if ":" in line else "="
        key, _, value = line.partition(sep)
        out[key.strip()] = value.strip()
    return out


def transform_value(field: dict, raw: Any) -> Any:
    """把表单里的原始值转换成 Hysteria YAML 需要的结构。"""
    kind = field.get("type")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if kind == "text":
        as_what = field.get("as")
        if as_what == "kv":
            return parse_kv(raw or "")
        if as_what == "lines":
            return parse_lines(raw or "")
        if as_what == "yaml":
            if not (raw or "").strip():
                return None
            return yaml.safe_load(raw)
        return raw
    if kind == "list":
        rows = raw if isinstance(raw, list) else []
        how = field.get("transform", "raw")
        first = field["columns"][0]["key"]
        if how == "strings":
            return [str(r.get(first, "")).strip() for r in rows if str(r.get(first, "")).strip()]
        if how == "userpass":
            return {str(r.get("user", "")).strip(): str(r.get("pass", "")) for r in rows if str(r.get("user", "")).strip()}
        if how == "outbounds":
            result = []
            for r in rows:
                name = str(r.get("name", "")).strip()
                otype = str(r.get("type", "direct")).strip()
                if not name:
                    continue
                item: dict[str, Any] = {"name": name, "type": otype}
                extra_text = str(r.get("extra", "")).strip()
                if extra_text:
                    parsed = json.loads(extra_text)
                    child = item.setdefault(otype, {})
                    child.update(parsed)
                result.append(item)
            return result
        return [dict(r) for r in rows]
    return raw


def active(field: dict, values: dict) -> bool:
    """条件字段（when）只在满足条件时写入配置。"""
    when = field.get("when")
    if not when:
        return True
    return all(values.get(k) == v for k, v in when.items())


def build_config(values: dict[str, Any], schema: dict | None = None) -> dict:
    """表单值 -> Hysteria 服务端配置字典。"""
    cfg: dict[str, Any] = {}
    for f in fields(schema):
        key = f["key"]
        if key.startswith("_") or not active(f, values):
            continue
        value = transform_value(f, values.get(key, f.get("default")))
        if value is None:
            continue
        set_path(cfg, key, value)

    # 类型选择格式的 type 字段由模式变量注入，表单里没有这些键
    auth_type = values.get("_authType", "password")
    set_path(cfg, "auth.type", auth_type)
    auth = cfg.get("auth", {})
    keep_auth = {"type", auth_type}
    cfg["auth"] = {k: v for k, v in auth.items() if k in keep_auth}

    tls_mode = values.get("_tlsMode", "acme")
    if tls_mode == "acme":
        cfg.pop("tls", None)
        acme = cfg.get("acme", {})
        verify = values.get("acme.type", "http")
        keep_acme = {"domains", "email", "ca", "dir", "type", "listenHost", verify}
        cfg["acme"] = {k: v for k, v in acme.items() if k in keep_acme}
        cfg["acme"]["type"] = verify
    else:
        cfg.pop("acme", None)

    if not values.get("_echEnabled"):
        cfg.pop("ech", None)

    if not values.get("_bandwidthEnabled"):
        cfg.pop("bandwidth", None)
    if not values.get("_congestionEnabled"):
        cfg.pop("congestion", None)

    obfs_type = values.get("_obfsType", "none")
    if obfs_type == "none":
        cfg.pop("obfs", None)
    else:
        obfs = cfg.get("obfs", {})
        cfg["obfs"] = {"type": obfs_type, obfs_type: obfs.get(obfs_type, {})}

    masq_type = values.get("_masqType", "none")
    if masq_type == "none":
        cfg.pop("masquerade", None)
    else:
        masq = cfg.get("masquerade", {})
        allowed = {"type", masq_type, "listenHTTP", "listenHTTPS", "forceHTTPS"}
        cfg["masquerade"] = {k: v for k, v in masq.items() if k in allowed}
        cfg["masquerade"]["type"] = masq_type
        if not values.get("_masqHttpEnabled"):
            for k in ("listenHTTP", "listenHTTPS", "forceHTTPS"):
                cfg["masquerade"].pop(k, None)

    if not values.get("_quicEnabled"):
        cfg.pop("quic", None)

    resolver_type = values.get("_resolverType", "none")
    if resolver_type == "none":
        cfg.pop("resolver", None)
    else:
        resolver = cfg.get("resolver", {})
        cfg["resolver"] = {"type": resolver_type, resolver_type: resolver.get(resolver_type, {})}

    if values.get("_sniffEnabled"):
        sniff = cfg.get("sniff", {})
        cfg["sniff"] = {"enable": True, **sniff}
    else:
        cfg.pop("sniff", None)

    acl_mode = values.get("_aclMode", "none")
    acl = cfg.get("acl", {})
    acl_keep = {"rules", "rulesFile", "inline", "file"}
    new_acl = {k: v for k, v in acl.items() if k in acl_keep and k == acl_mode}
    if acl_mode == "rules":
        for k in ("geoip", "geosite", "geoUpdateInterval"):
            if acl.get(k):
                new_acl[k] = acl[k]
    cfg["acl"] = new_acl

    if not values.get("_statsEnabled"):
        cfg.pop("trafficStats", None)
    if not values.get("_realmEnabled"):
        cfg.pop("realm", None)

    cfg = prune(cfg)

    # 表单未收录的字段通过高级 YAML 深合并，允许覆盖上面任何一层
    advanced = (values.get("_advancedYaml") or "").strip()
    if advanced:
        extra = yaml.safe_load(advanced)
        if not isinstance(extra, dict):
            raise ValueError("附加 YAML 的顶层必须是映射（key: value）")
        cfg = deep_merge(cfg, extra)

    return cfg


def dump_yaml(cfg: dict) -> str:
    return yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, default_flow_style=False, width=120)


def write_config(values: dict[str, Any], path: Path | str | None = None) -> Path:
    path = Path(path) if path else CONFIG_PATH
    cfg = build_config(values)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_yaml(cfg), encoding="utf-8")
    tmp.replace(path)
    return path


# ---------- 校验 ----------

def _port_part(listen: str) -> str:
    if listen.startswith("realm://"):
        return ""
    if listen.startswith("["):
        return listen.split("]:", 1)[1] if "]:" in listen else ""
    return listen.rsplit(":", 1)[1] if ":" in listen else listen


def validate(values: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    listen = str(values.get("listen") or "").strip()
    if not listen:
        errors.append("listen 不能为空")
    elif not listen.startswith("realm://") and PORT_RANGE_RE.search(_port_part(listen)):
        # 官方文档：端口范围监听仅支持 Linux（依赖 nftables/iptables），Windows 内核会失败
        errors.append("Windows 不支持端口范围 / 端口跳跃 listen，请改用单个端口，如 :443")

    tls_mode = values.get("_tlsMode")
    if tls_mode == "acme":
        domains = transform_value({"type": "list", "columns": [{"key": "domain"}], "transform": "strings"},
                                  values.get("acme.domains"))
        if not domains:
            errors.append("ACME 模式需要至少一个域名")
        if "@" not in str(values.get("acme.email") or ""):
            errors.append("ACME 邮箱格式不正确")
        if any(d.strip().lower() in ("example.com", "your.domain.net") for d in domains):
            errors.append("ACME 域名还是示例值，请换成你自己的域名")
        if values.get("acme.type") == "dns" and not str(values.get("acme.dns.name") or "").strip():
            errors.append("DNS-01 验证需要填写 DNS 提供商 name")
    elif tls_mode == "tls":
        if not str(values.get("tls.cert") or "").strip():
            errors.append("自有证书模式需要 cert 路径")
        if not str(values.get("tls.key") or "").strip():
            errors.append("自有证书模式需要 key 路径")
    else:
        errors.append("必须选择 acme 或 tls 之一")

    auth_type = values.get("_authType")
    if auth_type not in VALID_AUTH:
        errors.append("认证类型不合法")
    elif auth_type == "password" and not str(values.get("auth.password") or "").strip():
        errors.append("单密码模式需要填写 auth.password")
    elif auth_type == "userpass":
        users = transform_value({"type": "list", "columns": [{"key": "user"}], "transform": "userpass"},
                                values.get("auth.userpass"))
        if not users:
            errors.append("userpass 模式需要至少一个用户")
    elif auth_type == "http" and not str(values.get("auth.http.url") or "").strip():
        errors.append("HTTP 认证需要填写后端 URL")
    elif auth_type == "command" and not str(values.get("auth.command") or "").strip():
        errors.append("command 认证需要填写命令路径")

    if values.get("_bandwidthEnabled"):
        for side in ("bandwidth.up", "bandwidth.down"):
            text = str(values.get(side) or "").strip()
            if text and not BANDWIDTH_RE.match(text):
                errors.append(f"{side} 格式不正确，应形如 100 mbps / 1 gbps")

    obfs_type = values.get("_obfsType")
    if obfs_type in VALID_OBFS:
        if not str(values.get(f"obfs.{obfs_type}.password") or "").strip():
            errors.append(f"{obfs_type} 混淆需要密码")
        if obfs_type == "gecko":
            lo = int(values.get("obfs.gecko.minPacketSize") or 0)
            hi = int(values.get("obfs.gecko.maxPacketSize") or 0)
            if lo < 1 or hi < lo or hi > 2048:
                errors.append("Gecko 分片需满足 1 <= min <= max <= 2048")

    if values.get("_masqType") == "proxy" and not str(values.get("masquerade.proxy.url") or "").startswith(("http://", "https://")):
        errors.append("反代目标 URL 必须以 http:// 或 https:// 开头")

    if values.get("_quicEnabled"):
        for key in ("quic.initStreamReceiveWindow", "quic.maxStreamReceiveWindow",
                    "quic.initConnReceiveWindow", "quic.maxConnReceiveWindow"):
            if int(values.get(key) or 0) <= 0:
                errors.append(f"{key} 必须为正整数")

    if values.get("_aclMode") == "rules":
        raw = str(values.get("acl.rules") or "").strip()
        if not raw:
            errors.append("新版 ACL 规则为空")
        else:
            try:
                parsed = yaml.safe_load(raw)
                if not isinstance(parsed, list):
                    errors.append("acl.rules 必须是 YAML 列表")
            except yaml.YAMLError as exc:
                errors.append(f"acl.rules YAML 解析失败: {exc}")

    for row in values.get("outbounds") or []:
        otype = str(row.get("type", ""))
        if otype and otype not in VALID_OUTBOUND:
            errors.append(f"出站类型 {otype} 不支持（direct/socks5/http）")
        extra = str(row.get("extra") or "").strip()
        if extra:
            try:
                parsed = json.loads(extra)
                if not isinstance(parsed, dict):
                    errors.append("出站附加参数必须是 JSON 对象")
            except json.JSONDecodeError as exc:
                errors.append(f"出站附加参数 JSON 解析失败: {exc}")

    advanced = str(values.get("_advancedYaml") or "").strip()
    if advanced:
        try:
            parsed = yaml.safe_load(advanced)
            if not isinstance(parsed, dict):
                errors.append("附加 YAML 顶层必须是映射")
        except yaml.YAMLError as exc:
            errors.append(f"附加 YAML 解析失败: {exc}")

    return errors


def warnings(values: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if values.get("_statsEnabled") and not str(values.get("trafficStats.secret") or "").strip():
        out.append("流量统计 API 未设置 secret，任何能访问该端口的人都能查询/踢用户")
    if values.get("_masqType") == "none":
        out.append("未启用伪装，服务端对普通 HTTP 请求只返回 404")
    if str(values.get("_serverHost") or "").strip().lower() in ("example.com", "your.domain.net"):
        out.append("公网域名还是示例值，生成的分享链接无法连接")
    return out


# ---------- 客户端分享链接 ----------

def share_uri(values: dict[str, Any], name: str = "hysteria2") -> str:
    """按官方 URI Scheme 生成 hysteria2:// 链接。"""
    host = str(values.get("_serverHost") or "example.com").strip()
    listen = str(values.get("listen") or ":443")
    port = _port_part(listen) or "443"
    if listen.startswith("realm://"):
        return listen

    auth_type = values.get("_authType", "password")
    auth = ""
    if auth_type == "password":
        # 密码作为 userinfo 整体，特殊字符必须百分号编码
        auth = quote(str(values.get("auth.password") or ""), safe="")
    elif auth_type == "userpass":
        rows = values.get("auth.userpass") or []
        if rows:
            # 官方约定 userpass 用 username:password，冒号是结构分隔符，不能编码
            auth = (quote(str(rows[0].get("user", "")), safe="") + ":" +
                    quote(str(rows[0].get("pass", "")), safe=""))

    params: list[tuple[str, str]] = []
    obfs = values.get("_obfsType", "none")
    if obfs in VALID_OBFS:
        params.append(("obfs", obfs))
        params.append(("obfs-password", str(values.get(f"obfs.{obfs}.password") or "")))

    netloc = f"{auth}@{host}:{port}" if auth else f"{host}:{port}"
    uri = f"hysteria2://{netloc}/"
    if params:
        uri += "?" + urlencode(params, quote_via=quote)
    uri += "#" + quote(name, safe="")
    return uri


# ---------- 反向导入 ----------

def values_from_yaml(cfg: dict, schema: dict | None = None) -> dict[str, Any]:
    """把已有的 Hysteria config.yaml 尽量回填到表单；未识别的键放进高级 YAML。"""
    values = default_values(schema)
    known = set(field_map(schema).keys())
    for f in fields(schema):
        key = f["key"]
        if key.startswith("_"):
            continue
        value = get_path(cfg, key, None)
        if value is None:
            continue
        how = f.get("transform")
        as_what = f.get("as")
        if f.get("type") == "list":
            first = f["columns"][0]["key"]
            if how == "strings" and isinstance(value, list):
                values[key] = [{first: str(v)} for v in value]
            elif how == "userpass" and isinstance(value, dict):
                values[key] = [{"user": k, "pass": str(v)} for k, v in value.items()]
            elif how == "outbounds" and isinstance(value, list):
                rows = []
                for item in value:
                    otype = item.get("type", "direct")
                    extra = {k: v for k, v in item.get(otype, {}).items()} if isinstance(item.get(otype), dict) else {}
                    rows.append({"name": item.get("name", ""), "type": otype,
                                 "extra": json.dumps(extra, ensure_ascii=False) if extra else ""})
                values[key] = rows
            elif isinstance(value, list):
                values[key] = value
        elif as_what == "kv" and isinstance(value, dict):
            values[key] = "\n".join(f"{k}: {v}" for k, v in value.items())
        elif as_what in ("yaml", "lines") and not isinstance(value, str):
            values[key] = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
        elif f.get("type") == "int":
            values[key] = int(value)
        else:
            values[key] = value

    if isinstance(cfg.get("auth"), dict):
        values["_authType"] = cfg["auth"].get("type", "password")
    if "acme" in cfg:
        values["_tlsMode"] = "acme"
    elif "tls" in cfg:
        values["_tlsMode"] = "tls"
    values["_echEnabled"] = "ech" in cfg
    values["_bandwidthEnabled"] = "bandwidth" in cfg
    values["_congestionEnabled"] = "congestion" in cfg
    values["_obfsType"] = (cfg.get("obfs") or {}).get("type", "none") if isinstance(cfg.get("obfs"), dict) else "none"
    values["_masqType"] = (cfg.get("masquerade") or {}).get("type", "none") if isinstance(cfg.get("masquerade"), dict) else "none"
    values["_masqHttpEnabled"] = "listenHTTP" in (cfg.get("masquerade") or {})
    values["_quicEnabled"] = "quic" in cfg
    values["_resolverType"] = (cfg.get("resolver") or {}).get("type", "none") if isinstance(cfg.get("resolver"), dict) else "none"
    values["_sniffEnabled"] = bool((cfg.get("sniff") or {}).get("enable"))
    acl = cfg.get("acl") or {}
    for mode in ("rules", "rulesFile", "inline", "file"):
        if mode in acl:
            values["_aclMode"] = mode
            break
    values["_statsEnabled"] = "trafficStats" in cfg
    values["_realmEnabled"] = "realm" in cfg

    leftovers = {k: v for k, v in cfg.items() if k.split(".")[0] not in
                 {f.split(".")[0] for f in known if not f.startswith("_")}}
    if leftovers:
        values["_advancedYaml"] = dump_yaml(leftovers)
    return values
