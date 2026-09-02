"""
yuanbao_proto.py - Yuanbao WebSocket 协议编解码（纯 Python 实现）

协议层级：
  WebSocket frame
    └── ConnMsg (protobuf: trpc.yuanbao.conn_common.ConnMsg)
          ├── head: Head  (cmd_type, cmd, seq_no, msg_id, module, ...)
          └── data: bytes  (业务 payload，标准 protobuf)
                └── InboundMessagePush / SendC2CMessageReq / SendGroupMessageReq / ...
                      (trpc.yuanbao.yuanbao_conn.yuanbao_openclaw_proxy.*)

注意：conn 层（ConnMsg）本身是标准 protobuf，不是自定义二进制格式；conn.proto 注释里的
magic+head_len+body_len 格式仅用于 quic/tcp。WebSocket 每个 frame = 一条 ConnMsg protobuf。

实现方式：手写 varint / protobuf wire-format 编解码，不依赖第三方 protobuf 库。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

DEBUG_MODE = False


def _dbg(label: str, data: bytes) -> None:
    if DEBUG_MODE:
        hex_str = " ".join(f"{b:02x}" for b in data[:64])
        ellipsis = "..." if len(data) > 64 else ""
        logger.debug("[yuanbao_proto] %s (%dB): %s", label, len(data), hex_str + ellipsis)


# ============================================================
# 常量
# ============================================================

# conn 层消息类型（ConnMsg.Head.cmd_type）
PB_MSG_TYPES = {
    n: f"trpc.yuanbao.conn_common.{n}"
    for n in ("ConnMsg", "AuthBindReq", "AuthBindRsp", "PingReq", "PingRsp", "KickoutMsg", "DirectedPush", "PushMsg")
}

# cmd_type: 上行请求 / 请求回包 / 下行推送 / 推送 ACK
CMD_TYPE = {"Request": 0, "Response": 1, "Push": 2, "PushAck": 3}

# 内置命令字 / 模块名
CMD = {"AuthBind": "auth-bind", "Ping": "ping", "Kickout": "kickout", "UpdateMeta": "update-meta"}
MODULE = {"ConnAccess": "conn_access"}

# biz 层服务/方法映射。TS client 使用短名 'yuanbao_openclaw_proxy'（非完整包路径）。
_BIZ_PKG = "yuanbao_openclaw_proxy"
BIZ_SERVICES = {
    n: f"{_BIZ_PKG}.{n}"
    for n in (
        "InboundMessagePush",
        "SendC2CMessageReq", "SendC2CMessageRsp",
        "SendGroupMessageReq", "SendGroupMessageRsp",
        "QueryGroupInfoReq", "QueryGroupInfoRsp",
        "GetGroupMemberListReq", "GetGroupMemberListRsp",
        "SendPrivateHeartbeatReq", "SendPrivateHeartbeatRsp",
        "SendGroupHeartbeatReq", "SendGroupHeartbeatRsp",
    )
}

# openclaw instance_id（固定值 17）
HERMES_INSTANCE_ID = 17

# Reply Heartbeat 状态常量
WS_HEARTBEAT_RUNNING = 1
WS_HEARTBEAT_FINISH = 2

# ============================================================
# 序列号生成
# ============================================================

_seq_lock = threading.Lock()
_seq_counter = 0
_SEQ_MAX = 2 ** 32 - 1  # uint32 上限


def next_seq_no() -> int:
    """生成递增序列号（线程安全，溢出时归零）"""
    global _seq_counter
    with _seq_lock:
        val = _seq_counter
        _seq_counter = (_seq_counter + 1) & _SEQ_MAX
    return val


# ============================================================
# Protobuf wire-format 基础工具（手写，不依赖 google.protobuf）
# ============================================================

WT_VARINT = 0
WT_64BIT = 1
WT_LEN = 2
WT_32BIT = 5
_FIXED_SIZE = {WT_64BIT: 8, WT_32BIT: 4}


def _encode_varint(value: int) -> bytes:
    """将整数编码为 protobuf varint（负数按 64-bit two's complement）"""
    if value < 0:
        value = value & 0xFFFFFFFFFFFFFFFF
    out = []
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            break
    return bytes(out)


def _decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """从 data[pos:] 解码 varint，返回 (value, new_pos)"""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
        if shift >= 64:
            raise ValueError("varint too long")
    return result, pos


def _encode_field(field_number: int, wire_type: int, value: bytes) -> bytes:
    """编码一个 protobuf field（tag + value）"""
    return _encode_varint((field_number << 3) | wire_type) + value


def _encode_string(s: str) -> bytes:
    """length-prefixed UTF-8 string value"""
    encoded = s.encode("utf-8")
    return _encode_varint(len(encoded)) + encoded


def _encode_message(b: bytes) -> bytes:
    """length-prefixed bytes / 嵌套 message value"""
    return _encode_varint(len(b)) + b


# 完整 field 编码快捷方式：string / varint / 嵌套 message
def _s(fn: int, s: str) -> bytes:
    return _encode_field(fn, WT_LEN, _encode_string(s))


def _v(fn: int, n: int) -> bytes:
    return _encode_field(fn, WT_VARINT, _encode_varint(n))


def _m(fn: int, b: bytes) -> bytes:
    return _encode_field(fn, WT_LEN, _encode_message(b))


def _parse_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    """解析 message 的所有字段 → [(field_number, wire_type, raw_value)]；
    raw_value 为 int（VARINT）或 bytes（LEN / 64BIT / 32BIT）。"""
    fields = []
    pos = 0
    n = len(data)
    while pos < n:
        tag, pos = _decode_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == WT_VARINT:
            val, pos = _decode_varint(data, pos)
        elif wire_type == WT_LEN:
            length, pos = _decode_varint(data, pos)
            val = data[pos: pos + length]
            pos += length
        elif wire_type in _FIXED_SIZE:
            size = _FIXED_SIZE[wire_type]
            val = data[pos: pos + size]
            pos += size
        else:
            raise ValueError(f"unknown wire type {wire_type} at pos {pos - 1}")
        fields.append((field_number, wire_type, val))
    return fields


def _fields_to_dict(fields: list) -> dict[int, list]:
    """fields 列表 → {field_number: [(wire_type, value), ...]}（repeated 字段有多个）"""
    d: dict[int, list] = {}
    for fn, wt, val in fields:
        d.setdefault(fn, []).append((wt, val))
    return d


def _parse_dict(data: bytes) -> dict[int, list]:
    return _fields_to_dict(_parse_fields(data))


def _first(fdict: dict, fn: int, wt: int):
    """第一个 wire type 匹配的字段值，无则 None"""
    entries = fdict.get(fn)
    if entries and entries[0][0] == wt:
        return entries[0][1]
    return None


def _get_string(fdict: dict, fn: int, default: str = "") -> str:
    val = _first(fdict, fn, WT_LEN)
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")
    return default


def _get_varint(fdict: dict, fn: int, default: int = 0) -> int:
    val = _first(fdict, fn, WT_VARINT)
    return val if isinstance(val, int) else default


def _get_bytes(fdict: dict, fn: int, default: bytes = b"") -> bytes:
    val = _first(fdict, fn, WT_LEN)
    return bytes(val) if isinstance(val, (bytes, bytearray)) else default


def _get_repeated_bytes(fdict: dict, fn: int) -> list[bytes]:
    return [bytes(val) for wt, val in fdict.get(fn, []) if wt == WT_LEN]


# 字段表驱动编解码：spec = [(field_number, key, kind)]，kind:
#   "s" string（编码时 str(v)）  "r" string（原值）  "i" varint（编码时 int(v)）
# 编码跳过 falsy 值；解码只保留 truthy 值。spec 顺序即 wire 顺序和 dict 插入顺序。
_STR_KINDS = ("s", "r")


def _encode_spec(obj: dict, spec: list) -> bytes:
    buf = b""
    for fn, key, kind in spec:
        v = obj.get(key, "" if kind in _STR_KINDS else 0)
        if v:
            buf += _s(fn, str(v) if kind == "s" else v) if kind in _STR_KINDS else _v(fn, int(v))
    return buf


def _decode_spec(fdict: dict, spec: list) -> dict:
    out: dict = {}
    for fn, key, kind in spec:
        v = _get_string(fdict, fn) if kind in _STR_KINDS else _get_varint(fdict, fn)
        if v:
            out[key] = v
    return out


# ============================================================
# ConnMsg 层编解码
# ============================================================
#   message Head { uint32 cmd_type=1; string cmd=2; uint32 seq_no=3; string msg_id=4;
#                  string module=5; bool need_ack=6; ... int32 status=10; }
#   message ConnMsg { Head head=1; bytes data=2; }


def _encode_head(
    cmd_type: int, cmd: str, seq_no: int, msg_id: str, module: str, need_ack: bool = False, status: int = 0,
) -> bytes:
    buf = b""
    if cmd_type != 0:
        buf += _v(1, cmd_type)
    if cmd:
        buf += _s(2, cmd)
    if seq_no != 0:
        buf += _v(3, seq_no)
    if msg_id:
        buf += _s(4, msg_id)
    if module:
        buf += _s(5, module)
    if need_ack:
        buf += _v(6, 1)
    if status != 0:
        buf += _v(10, status & 0xFFFFFFFFFFFFFFFF)
    return buf


def _decode_head(data: bytes) -> dict:
    fdict = _parse_dict(data)
    return {
        "cmd_type": _get_varint(fdict, 1, 0),
        "cmd": _get_string(fdict, 2, ""),
        "seq_no": _get_varint(fdict, 3, 0),
        "msg_id": _get_string(fdict, 4, ""),
        "module": _get_string(fdict, 5, ""),
        "need_ack": bool(_get_varint(fdict, 6, 0)),
        "status": _get_varint(fdict, 10, 0),
    }


def _conn_msg(label: str, cmd_type: int, cmd: str, seq_no: int, msg_id: str, module: str, data: bytes, need_ack: bool) -> bytes:
    buf = _m(1, _encode_head(cmd_type, cmd, seq_no, msg_id, module, need_ack))
    if data:
        buf += _m(2, data)
    _dbg(label, buf)
    return buf


def encode_conn_msg(msg_type: int, seq_no: int, data: bytes) -> bytes:
    """编码 ConnMsg（简化接口：仅 cmd_type + seq_no + payload）"""
    return _conn_msg("encode_conn_msg", msg_type, "", seq_no, "", "", data, False)


def encode_conn_msg_full(
    cmd_type: int, cmd: str, seq_no: int, msg_id: str, module: str, data: bytes, need_ack: bool = False,
) -> bytes:
    """编码完整的 ConnMsg（含 cmd/msg_id/module 等 head 字段）"""
    return _conn_msg("encode_conn_msg_full", cmd_type, cmd, seq_no, msg_id, module, data, need_ack)


def decode_conn_msg(data: bytes) -> dict:
    """解码 ConnMsg → {msg_type, seq_no, data, head}（head 为完整 Head dict）"""
    _dbg("decode_conn_msg", data)
    fdict = _parse_dict(data)
    head = _decode_head(_get_bytes(fdict, 1))
    return {"msg_type": head["cmd_type"], "seq_no": head["seq_no"], "data": _get_bytes(fdict, 2), "head": head}


# ============================================================
# BizMsg 层：业务 body 包装成 ConnMsg（head.cmd = method, head.module = service）
# 与 conn-codec.ts buildBusinessConnMsg(cmd, module, bizData, msgId) 行为一致。
# ============================================================


def encode_biz_msg(service: str, method: str, req_id: str, body: bytes) -> bytes:
    """将已编码的业务 protobuf 包装为可直接发送的 ConnMsg bytes"""
    return encode_conn_msg_full(
        cmd_type=CMD_TYPE["Request"], cmd=method, seq_no=next_seq_no(), msg_id=req_id, module=service, data=body,
    )


def decode_biz_msg(data: bytes) -> dict:
    """解码 ConnMsg → {service, method, req_id, body, is_response, head}"""
    result = decode_conn_msg(data)
    head = result["head"]
    return {
        "service": head["module"],
        "method": head["cmd"],
        "req_id": head["msg_id"],
        "body": result["data"],
        "is_response": head["cmd_type"] == CMD_TYPE["Response"],
        "head": head,
    }


def _biz_request(method: str, prefix: str, body: bytes, msg_id: str = "") -> bytes:
    """biz 请求 ConnMsg；req_id 为 msg_id，空则 '<prefix>_<seq>'（seq 在 conn seq_no 之前分配）"""
    req_id = msg_id or f"{prefix}_{next_seq_no()}"
    return encode_biz_msg(service=_BIZ_PKG, method=method, req_id=req_id, body=body)


# ============================================================
# 业务 protobuf 消息编解码（biz payload）
# ============================================================

# MsgContent：1 text, 2 uuid, 3 image_format, 4 data, 5 desc, 6 ext, 7 sound,
#   8 image_info_array (repeated), 9 index, 10 url, 11 file_size, 12 file_name,
#   999 ext_map (map<string,string>: repeated entry{1 key, 2 value})
#   ext_map key 格式 wexin_forward_msg_[forward_msg_id]_[userid]，value 为
#   base64(ForwardMsgData protobuf)（不是 JSON），用 decode_forward_msg_data() 解析。
_MSG_CONTENT_SPEC = [
    (1, "text", "s"), (2, "uuid", "s"), (4, "data", "s"), (5, "desc", "s"),
    (6, "ext", "s"), (7, "sound", "s"), (10, "url", "s"), (12, "file_name", "s"),
    (3, "image_format", "i"), (9, "index", "i"), (11, "file_size", "i"),
]
_IMAGE_INFO_SPEC = [(1, "type", "i"), (2, "size", "i"), (3, "width", "i"), (4, "height", "i"), (5, "url", "r")]
_MAP_ENTRY_SPEC = [(1, "key", "s"), (2, "value", "s")]


def _encode_msg_content(content: dict) -> bytes:
    buf = _encode_spec(content, _MSG_CONTENT_SPEC)
    for img in content.get("image_info_array") or []:
        buf += _m(8, _encode_spec(img, _IMAGE_INFO_SPEC))
    ext_map = content.get("ext_map")
    if isinstance(ext_map, dict):
        for k, v in ext_map.items():
            buf += _m(999, _encode_spec({"key": str(k), "value": str(v)}, _MAP_ENTRY_SPEC))
    return buf


def _decode_msg_content(data: bytes) -> dict:
    fdict = _parse_dict(data)
    content = _decode_spec(fdict, _MSG_CONTENT_SPEC)
    imgs = [img for img in (_decode_spec(_parse_dict(b), _IMAGE_INFO_SPEC) for b in _get_repeated_bytes(fdict, 8)) if img]
    if imgs:
        content["image_info_array"] = imgs
    ext_map: dict[str, str] = {}
    for entry_bytes in _get_repeated_bytes(fdict, 999):
        efdict = _parse_dict(entry_bytes)
        k = _get_string(efdict, 1)
        if k:
            ext_map[k] = _get_string(efdict, 2)
    if ext_map:
        content["ext_map"] = ext_map
    return content


# MsgBodyElement：1 msg_type (string, e.g. "TIMTextElem"), 2 msg_content (MsgContent)


def _encode_msg_body_element(element: dict) -> bytes:
    buf = b""
    msg_type = element.get("msg_type", "")
    if msg_type:
        buf += _s(1, msg_type)
    content = element.get("msg_content", {})
    if content:
        buf += _m(2, _encode_msg_content(content))
    return buf


def _decode_msg_body_element(data: bytes) -> dict:
    fdict = _parse_dict(data)
    content_bytes = _get_bytes(fdict, 2)
    return {"msg_type": _get_string(fdict, 1, ""), "msg_content": _decode_msg_content(content_bytes) if content_bytes else {}}


def _encode_msg_body(fn: int, msg_body: list) -> bytes:
    return b"".join(_m(fn, _encode_msg_body_element(el)) for el in msg_body)


def _encode_log_ext(trace_id: str) -> bytes:
    """LogInfoExt：1 trace_id"""
    return _s(1, trace_id) if trace_id else b""


# ============================================================
# 入站消息解析
# ============================================================
# InboundMessagePush: 1 callback_command, 2 from_account, 3 to_account, 4 sender_nickname,
#   5 group_id, 6 group_code, 7 group_name, 8 msg_seq, 9 msg_random, 10 msg_time, 11 msg_key,
#   12 msg_id, 13 msg_body (repeated MsgBodyElement), 14 cloud_custom_data, 15 event_time,
#   16 bot_owner_id, 17 recall_msg_seq_list (repeated ImMsgSeq{1 msg_seq, 2 msg_id}),
#   18 claw_msg_type, 19 private_from_group_code, 20 log_ext (LogInfoExt{1 trace_id})


def decode_inbound_push(data: bytes) -> Optional[dict]:
    """解析 InboundMessagePush biz payload。

    Returns: 上述字段的 dict（空值已过滤，msg_body / msg_seq 始终保留；
    recall_msg_seq_list 为 [{msg_seq, msg_id}] 或 None），解析失败返回 None。
    """
    try:
        _dbg("decode_inbound_push input", data)
        fdict = _parse_dict(data)
        log_ext_bytes = _get_bytes(fdict, 20)
        result: dict = {
            "callback_command": _get_string(fdict, 1),
            "from_account": _get_string(fdict, 2),
            "to_account": _get_string(fdict, 3),
            "sender_nickname": _get_string(fdict, 4),
            "group_id": _get_string(fdict, 5),
            "group_code": _get_string(fdict, 6),
            "group_name": _get_string(fdict, 7),
            "msg_seq": _get_varint(fdict, 8),
            "msg_random": _get_varint(fdict, 9),
            "msg_time": _get_varint(fdict, 10),
            "msg_key": _get_string(fdict, 11),
            "msg_id": _get_string(fdict, 12),
            "msg_body": [_decode_msg_body_element(b) for b in _get_repeated_bytes(fdict, 13)],
            "cloud_custom_data": _get_string(fdict, 14),
            "event_time": _get_varint(fdict, 15),
            "bot_owner_id": _get_string(fdict, 16),
            "recall_msg_seq_list": [
                {"msg_seq": _get_varint(d, 1), "msg_id": _get_string(d, 2)}
                for d in map(_parse_dict, _get_repeated_bytes(fdict, 17))
            ] or None,
            "claw_msg_type": _get_varint(fdict, 18),
            "private_from_group_code": _get_string(fdict, 19),
            "trace_id": _get_string(_parse_dict(log_ext_bytes), 1) if log_ext_bytes else "",
        }
        return {k: v for k, v in result.items() if v or k in {"msg_body", "msg_seq"}}
    except Exception as e:
        if DEBUG_MODE:
            logger.debug("[yuanbao_proto] decode_inbound_push failed: %s", e)
        return None


# ============================================================
# WeChat forwarded chat-history parsing (ForwardMsgData)
# ============================================================
# ext_map["wexin_forward_msg_<id>_<userid>"] = base64(ForwardMsgData) — protobuf, NOT JSON.
# Verified against live captures:
#   ForwardMsgData { uint32 sub_type=1 (1 = WeChat chat-history forward); uint32 begin_time=2;
#                    uint32 end_time=3; string nick_name=4 (forwarder); repeated ForwardMsg msg=5 }
#   ForwardMsg     { string sender=1; uint32 time=2; string plainText=3; repeated MsgContent msgContent=4 }
#   MsgContent     { uint32 type=1 (1=TEXT, 2=MULTIMEDIA, 3=nested forward); string text=2;
#                    repeated Multimedia multimedia=3 }
#   Multimedia     { string type=1 (image/file/document/url/video); string url=2; string file_name=4;
#                    uint32 file_size=5; uint32 width=6; uint32 height=7;
#                    string media_id=15 (usable directly as a ybres RID); string res_type=24 }
_FORWARD_MULTIMEDIA_SPEC = [(1, "type", "s"), (2, "url", "s"), (4, "file_name", "s"), (5, "file_size", "i"), (15, "media_id", "s")]


def _decode_forward_msg_content(data: bytes) -> dict:
    """MsgContent → {type, text?, multimedia?}（shape 与 _format_multimedia 对齐）"""
    fdict = _parse_dict(data)
    content: dict = {"type": _get_varint(fdict, 1)}
    text = _get_string(fdict, 2)
    if text:
        content["text"] = text
    multimedia = [_decode_spec(_parse_dict(b), _FORWARD_MULTIMEDIA_SPEC) for b in _get_repeated_bytes(fdict, 3)]
    if multimedia:
        content["multimedia"] = multimedia
    return content


def _decode_forward_msg(data: bytes) -> dict:
    fdict = _parse_dict(data)
    return {
        "sender": _get_string(fdict, 1),
        "time": _get_varint(fdict, 2),
        "plainText": _get_string(fdict, 3),
        "msgContent": [_decode_forward_msg_content(b) for b in _get_repeated_bytes(fdict, 4)],
    }


def decode_forward_msg_data(data: bytes) -> Optional[dict]:
    """Parse ForwardMsgData protobuf bytes (the base64-decoded ext_map value).

    Returns the ``sub_type`` / ``nick_name`` / ``msg`` structure consumed by
    ``ForwardedRecordsParseMiddleware.build_forward_text``; ``None`` on parse failure.
    """
    try:
        fdict = _parse_dict(data)
        return {
            "sub_type": _get_varint(fdict, 1),
            "begin_time": _get_varint(fdict, 2),
            "end_time": _get_varint(fdict, 3),
            "nick_name": _get_string(fdict, 4),
            "msg": [_decode_forward_msg(b) for b in _get_repeated_bytes(fdict, 5)],
        }
    except Exception as e:
        if DEBUG_MODE:
            logger.debug("[yuanbao_proto] decode_forward_msg_data failed: %s", e)
        return None


# ============================================================
# Outbound message encoding
# ============================================================


def encode_send_c2c_message(
    to_account: str,
    msg_body: list,
    from_account: str,
    msg_id: str = "",
    msg_random: int = 0,
    msg_seq: Optional[int] = None,
    group_code: str = "",
    trace_id: str = "",
) -> bytes:
    """Encode a SendC2CMessageReq and return the full ConnMsg bytes (ready to send over WebSocket).

    SendC2CMessageReq: 1 msg_id, 2 to_account, 3 from_account, 4 msg_random,
      5 msg_body (repeated MsgBodyElement), 6 group_code, 7 msg_seq, 8 log_ext.
    msg_body items are {"msg_type": str, "msg_content": dict}; msg_id doubles as req_id when set;
    group_code is filled for the "private chat originating from a group" case.
    """
    biz_bytes = b""
    if msg_id:
        biz_bytes += _s(1, msg_id)
    biz_bytes += _s(2, to_account)
    if from_account:
        biz_bytes += _s(3, from_account)
    if msg_random:
        biz_bytes += _v(4, msg_random)
    biz_bytes += _encode_msg_body(5, msg_body)
    if group_code:
        biz_bytes += _s(6, group_code)
    if msg_seq is not None:
        biz_bytes += _v(7, msg_seq)
    if trace_id:
        biz_bytes += _m(8, _encode_log_ext(trace_id))
    _dbg("encode_send_c2c biz payload", biz_bytes)
    return _biz_request("send_c2c_message", "c2c", biz_bytes, msg_id)


def encode_send_group_message(
    group_code: str,
    msg_body: list,
    from_account: str,
    msg_id: str = "",
    to_account: str = "",
    random: str = "",
    msg_seq: Optional[int] = None,
    ref_msg_id: str = "",
    trace_id: str = "",
) -> bytes:
    """Encode a SendGroupMessageReq and return the full ConnMsg bytes (ready to send over WebSocket).

    SendGroupMessageReq: 1 msg_id, 2 group_code, 3 from_account, 4 to_account (usually empty),
      5 random (string), 6 msg_body, 7 ref_msg_id (quoted message), 8 msg_seq, 9 log_ext.
    """
    biz_bytes = b""
    if msg_id:
        biz_bytes += _s(1, msg_id)
    biz_bytes += _s(2, group_code)
    if from_account:
        biz_bytes += _s(3, from_account)
    if to_account:
        biz_bytes += _s(4, to_account)
    if random:
        biz_bytes += _s(5, random)
    biz_bytes += _encode_msg_body(6, msg_body)
    if ref_msg_id:
        biz_bytes += _s(7, ref_msg_id)
    if msg_seq is not None:
        biz_bytes += _v(8, msg_seq)
    if trace_id:
        biz_bytes += _m(9, _encode_log_ext(trace_id))
    _dbg("encode_send_group biz payload", biz_bytes)
    return _biz_request("send_group_message", "grp", biz_bytes, msg_id)


# ============================================================
# AuthBind / Ping / PushAck
# ============================================================


def encode_auth_bind(
    biz_id: str,
    uid: str,
    source: str,
    token: str,
    msg_id: str,
    app_version: str = "",
    operation_system: str = "",
    bot_version: str = "",
    route_env: str = "",
) -> bytes:
    """构造 auth-bind 请求 ConnMsg bytes。

    AuthBindReq: 1 biz_id, 2 auth_info (AuthInfo{1 uid, 2 source, 3 token}),
      3 device_info (DeviceInfo{1 app_version, 2 app_operation_system, 10 instance_id, 24 bot_version}),
      5 env_name
    """
    auth_buf = _s(1, uid) + _s(2, source) + _s(3, token)
    dev_buf = b""
    if app_version:
        dev_buf += _s(1, app_version)
    if operation_system:
        dev_buf += _s(2, operation_system)
    dev_buf += _s(10, str(HERMES_INSTANCE_ID))
    if bot_version:
        dev_buf += _s(24, bot_version)
    req_buf = _s(1, biz_id) + _m(2, auth_buf) + _m(3, dev_buf)
    if route_env:
        req_buf += _s(5, route_env)
    return encode_conn_msg_full(
        cmd_type=CMD_TYPE["Request"], cmd=CMD["AuthBind"], seq_no=next_seq_no(), msg_id=msg_id,
        module=MODULE["ConnAccess"], data=req_buf,
    )


def encode_ping(msg_id: str) -> bytes:
    """构造 ping 请求 ConnMsg bytes（PingReq 为空消息）"""
    return encode_conn_msg_full(
        cmd_type=CMD_TYPE["Request"], cmd=CMD["Ping"], seq_no=next_seq_no(), msg_id=msg_id,
        module=MODULE["ConnAccess"], data=b"",
    )


def encode_push_ack(original_head: dict) -> bytes:
    """构造 push ACK 回包（回显原 head 的 cmd / msg_id / module）"""
    return encode_conn_msg_full(
        cmd_type=CMD_TYPE["PushAck"], cmd=original_head.get("cmd", ""), seq_no=next_seq_no(),
        msg_id=original_head.get("msg_id", ""), module=original_head.get("module", ""), data=b"",
    )


# ============================================================
# Heartbeat / 群信息 / 群成员列表
# ============================================================


def encode_send_private_heartbeat(from_account: str, to_account: str, heartbeat: int = WS_HEARTBEAT_RUNNING) -> bytes:
    """SendPrivateHeartbeatReq{1 from_account, 2 to_account, 3 heartbeat (RUNNING=1, FINISH=2)} → ConnMsg bytes"""
    buf = _s(1, from_account) + _s(2, to_account) + _v(3, heartbeat)
    return _biz_request("send_private_heartbeat", "hb_priv", buf)


def encode_send_group_heartbeat(
    from_account: str, group_code: str, heartbeat: int = WS_HEARTBEAT_RUNNING, send_time: int = 0,
) -> bytes:
    """SendGroupHeartbeatReq{1 from_account, 2 to_account (群场景留空), 3 group_code,
    4 send_time (ms; 0 → now), 5 heartbeat} → ConnMsg bytes"""
    import time as _time
    ts = send_time or int(_time.time() * 1000)
    buf = _s(1, from_account) + _s(2, "") + _s(3, group_code) + _v(4, ts) + _v(5, heartbeat)
    return _biz_request("send_group_heartbeat", "hb_grp", buf)


def encode_query_group_info(group_code: str) -> bytes:
    """QueryGroupInfoReq{1 group_code} → ConnMsg bytes"""
    return _biz_request("query_group_info", "qgi", _s(1, group_code))


def decode_query_group_info_rsp(data: bytes) -> Optional[dict]:
    """解码 QueryGroupInfoRsp（对齐 TS biz-codec / member.ts queryGroupInfo）。

    QueryGroupInfoRsp{1 code, 2 message, 3 GroupInfo{1 group_name, 2 group_owner_user_id,
    3 group_owner_nickname, 4 group_size}} → {code, message?, group_name, owner_id,
    owner_nickname, member_count}，解析失败返回 None。
    """
    try:
        fdict = _parse_dict(data)
        result: dict = {"code": _get_varint(fdict, 1, 0)}
        msg = _get_string(fdict, 2)
        if msg:
            result["message"] = msg
        # field 3 taken regardless of wire type; non-bytes payloads fall back to defaults
        gi_entries = fdict.get(3, [])
        gi_bytes = gi_entries[0][1] if gi_entries else b""
        gi = _parse_dict(gi_bytes) if gi_bytes and isinstance(gi_bytes, (bytes, bytearray)) else {}
        result["group_name"] = _get_string(gi, 1)
        result["owner_id"] = _get_string(gi, 2)
        result["owner_nickname"] = _get_string(gi, 3)
        result["member_count"] = _get_varint(gi, 4, 0)
        return result
    except Exception:
        return None


def encode_get_group_member_list(group_code: str, offset: int = 0, limit: int = 200) -> bytes:
    """GetGroupMemberListReq{1 group_code, 2 offset, 3 limit} → ConnMsg bytes"""
    buf = _s(1, group_code)
    if offset:
        buf += _v(2, offset)
    buf += _v(3, limit)
    return _biz_request("get_group_member_list", "gml", buf)


def decode_get_group_member_list_rsp(data: bytes) -> Optional[dict]:
    """解码 GetGroupMemberListRsp{1 code, 2 message, 3 members (repeated MemberInfo), 4 next_offset,
    5 is_complete}；MemberInfo{1 user_id, 2 nickname, 3 role (0=member,1=admin,2=owner),
    4 join_time, 5 name_card (群昵称)}。member dict 过滤空值但保留 role；解析失败返回 None。
    """
    try:
        fdict = _parse_dict(data)
        members = []
        for mdict in map(_parse_dict, _get_repeated_bytes(fdict, 3)):
            member = {
                "user_id": _get_string(mdict, 1),
                "nickname": _get_string(mdict, 2),
                "role": _get_varint(mdict, 3),
                "join_time": _get_varint(mdict, 4),
                "name_card": _get_string(mdict, 5),
            }
            members.append({k: v for k, v in member.items() if v or k == "role"})
        return {
            "code": _get_varint(fdict, 1, 0),
            "message": _get_string(fdict, 2),
            "members": members,
            "next_offset": _get_varint(fdict, 4),
            "is_complete": bool(_get_varint(fdict, 5)),
        }
    except Exception:
        return None
