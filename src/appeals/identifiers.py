"""案件编号生成：与契约样例风格一致（09302-018-序号）。"""
from __future__ import annotations


def generate_case_id(repo, now) -> str:
    serial = repo.next_case_serial()
    return f"{now:%m%d%y}-{serial:03d}-A"
