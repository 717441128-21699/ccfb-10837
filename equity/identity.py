"""名称归一化与实体消歧 —— 解决需求 3、4。

- 公司：全角/半角括号统一、去空白、去掉“有限公司/有限责任公司”等后缀差异，
  让“字节跳动（北京）有限公司”和“字节跳动(北京)有限责任公司”归一到同一键。
- 自然人：同名不同人靠证件尾号区分；没有证件尾号时只按姓名归并（兜底）。
"""
from __future__ import annotations

import re
import unicodedata

# 公司组织形式后缀（去掉之后再比较）
_COMPANY_SUFFIXES = [
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "集团公司",
    "有限公司",
    "股份公司",
    "集团",
    "公司",
]

# 地区/修饰前缀里不影响主体识别的词（谨慎，仅做空白与符号层面的归一）
_PUNCT_MAP = str.maketrans({
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
    "，": ",",
    "。": ".",
    "：": ":",
    "；": ";",
    "　": "",
    " ": "",
    " ": "",
    "﹝": "(",
    "﹞": ")",
})


def normalize_company_name(name: str) -> str:
    """公司名 → 归一化比较键。"""
    if not name:
        return ""
    # NFKC 会把全角字母/数字转半角；括号我们自己统一
    s = unicodedata.normalize("NFKC", name).translate(_PUNCT_MAP)
    s = re.sub(r"\s+", "", s)
    # 去尾部组织形式后缀（可能套两层，如“xx集团有限公司”）
    changed = True
    while changed:
        changed = False
        for suf in _COMPANY_SUFFIXES:
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)]
                changed = True
                break
    return s.lower()


def normalize_person_name(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).translate(_PUNCT_MAP)
    return re.sub(r"\s+", "", s)


def person_key(name: str, id_tail: str | None) -> str:
    """自然人消歧键：姓名归一 + 证件尾号。

    有尾号时尾号是硬区分（同名不同人）；
    无尾号时退回姓名键，由调用方决定是否允许合并。
    """
    n = normalize_person_name(name)
    tail = (id_tail or "").strip().upper()
    return f"P::{n}::{tail}" if tail else f"P::{n}::?"


def company_key(name: str, credit_code: str | None = None) -> str:
    """公司消歧键：优先统一社会信用代码，没有再退归一化名。"""
    code = (credit_code or "").strip().upper()
    if code:
        return f"C::{code}"
    return f"N::{normalize_company_name(name)}"


def name_variants(name: str) -> list[str]:
    """生成常见写法变体，用于本地模糊匹配/搜索召回。"""
    base = unicodedata.normalize("NFKC", name)
    out = {base, base.replace("(", "（").replace(")", "）")}
    stripped = re.sub(r"\s+", "", base)
    out.add(stripped)
    out.add(stripped.replace("(", "（").replace(")", "）"))
    return [v for v in out if v]
