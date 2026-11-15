"""稠密线性方程组求解（高斯-约当消元，带部分主元）。

环持股/交叉持股下最终持股比例满足线性方程组 ``(I - W) x = b``，
标准库没有 numpy，这里实现一个数值稳定的小规模求解器：
穿透图的节点数通常在几千以内，部分主元高斯消元足够且确定性强。
"""
from __future__ import annotations

import math


class SingularMatrixError(RuntimeError):
    pass


def solve_linear(a: list[list[float]], b: list[float],
                 *, tol: float = 1e-12) -> list[float]:
    """解 Ax=b。会复制输入，不修改原矩阵。"""
    n = len(a)
    if n == 0:
        return []
    if len(b) != n or any(len(row) != n for row in a):
        raise ValueError("矩阵维度不匹配")
    # 增广矩阵
    m = [list(a[i]) + [b[i]] for i in range(n)]
    scale = max(
        (abs(m[i][j]) for i in range(n) for j in range(n)), default=1.0)
    pivot_tol = tol * max(1.0, scale)

    for col in range(n):
        # 部分主元：选该列绝对值最大的行
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) <= pivot_tol:
            raise SingularMatrixError(f"第 {col} 列找不到有效主元")
        m[col], m[pivot] = m[pivot], m[col]
        pivot_val = m[col][col]
        # 归一化主元行
        for j in range(col, n + 1):
            m[col][j] /= pivot_val
        # 消去其它所有行
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col]
            if abs(factor) <= pivot_tol:
                continue
            for j in range(col, n + 1):
                m[r][j] -= factor * m[col][j]
    return [m[i][n] for i in range(n)]


def residual_norm(a: list[list[float]], x: list[float],
                  b: list[float]) -> float:
    """残差 ||Ax-b||_∞，供调用方校验解的质量。"""
    worst = 0.0
    for i, row in enumerate(a):
        acc = sum(row[j] * x[j] for j in range(len(x))) - b[i]
        worst = max(worst, abs(acc))
    return worst


def is_close(x: float, y: float, *, rel: float = 1e-9,
             abs_tol: float = 1e-9) -> bool:
    return math.isclose(x, y, rel_tol=rel, abs_tol=abs_tol)
