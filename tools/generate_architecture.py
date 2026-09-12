#!/usr/bin/env python3
"""Generate the editable Legal Case OS v1.2.2 public architecture SVG.

The PNG preview is rendered separately so this generator stays dependency-free.
"""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path


WIDTH = 2400
HEIGHT = 1760


def box(
    x: int,
    y: int,
    w: int,
    h: int,
    title: str,
    lines: list[str],
    fill: str,
    stroke: str,
    *,
    title_fill: str = "#10233f",
    body_fill: str = "#29405f",
    rx: int = 16,
) -> str:
    items = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>',
        f'<text x="{x + 20}" y="{y + 34}" class="box-title" fill="{title_fill}">{escape(title)}</text>',
    ]
    for index, line in enumerate(lines):
        items.append(
            f'<text x="{x + 20}" y="{y + 65 + index * 25}" class="body" fill="{body_fill}">{escape(line)}</text>'
        )
    return "\n".join(items)


def section(x: int, y: int, w: int, h: int, title: str, fill: str = "#ffffff", stroke: str = "#c8d4e3") -> str:
    return "\n".join(
        [
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="22" fill="{fill}" stroke="{stroke}" stroke-width="2"/>',
            f'<text x="{x + 26}" y="{y + 40}" class="section-title">{escape(title)}</text>',
        ]
    )


def arrow(x1: int, y1: int, x2: int, y2: int, cls: str = "flow") -> str:
    return f'<path d="M{x1} {y1} L{x2} {y2}" class="{cls}"/>'


def poly(points: list[tuple[int, int]], cls: str = "flow") -> str:
    joined = " L".join(f"{x} {y}" for x, y in points)
    return f'<path d="M{joined}" class="{cls}"/>'


def pill(x: int, y: int, w: int, label: str, fill: str, text_fill: str = "#10233f") -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="34" rx="17" fill="{fill}"/>'
        f'<text x="{x + w / 2}" y="{y + 23}" class="pill" fill="{text_fill}" text-anchor="middle">{escape(label)}</text>'
    )


def build_svg() -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">',
        '<title id="title">个人法律办案操作系统 v1.2.2 公共版总体架构</title>',
        '<desc id="desc">展示自然语言编译、案件内状态、个人模板学习、复杂文书合成、十四个公开法律技能与确定性执行底座。</desc>',
        '''<defs>
          <marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#64748b"/></marker>
          <marker id="arrow-blue" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#2563eb"/></marker>
          <marker id="arrow-orange" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#ea580c"/></marker>
          <marker id="arrow-purple" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#7c3aed"/></marker>
          <style>
            .title{font:700 38px 'Microsoft YaHei','PingFang SC',sans-serif;fill:#10233f}
            .subtitle{font:400 18px 'Microsoft YaHei','PingFang SC',sans-serif;fill:#52647d}
            .section-title{font:700 22px 'Microsoft YaHei','PingFang SC',sans-serif;fill:#10233f}
            .box-title{font:700 18px 'Microsoft YaHei','PingFang SC',sans-serif}
            .body{font:400 15px 'Microsoft YaHei','PingFang SC',sans-serif}
            .small{font:400 13px 'Microsoft YaHei','PingFang SC',sans-serif;fill:#64748b}
            .pill{font:700 14px 'Microsoft YaHei','PingFang SC',sans-serif}
            .gate-title{font:700 15px 'Microsoft YaHei','PingFang SC',sans-serif;fill:#5b21b6}
            .gate-sub{font:400 13px 'Microsoft YaHei','PingFang SC',sans-serif;fill:#6d28d9}
            .flow{fill:none;stroke:#64748b;stroke-width:2.4;marker-end:url(#arrow)}
            .flow-blue{fill:none;stroke:#2563eb;stroke-width:3;marker-end:url(#arrow-blue)}
            .flow-orange{fill:none;stroke:#ea580c;stroke-width:2.5;stroke-dasharray:8 6;marker-end:url(#arrow-orange)}
            .control{fill:none;stroke:#7c3aed;stroke-width:2.3;stroke-dasharray:8 6;marker-end:url(#arrow-purple)}
            .read{fill:none;stroke:#0891b2;stroke-width:2.3;stroke-dasharray:5 5;marker-end:url(#arrow)}
          </style>
        </defs>''',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#f6f8fc"/>',
        '<text x="60" y="56" class="title">个人法律办案操作系统 v1.2.1 · 当前总体架构</text>',
        '<text x="60" y="88" class="subtitle">四块稳定内核：自然语言总控 · 案件状态 · 个人模板学习 · 法源约束下的文书合成</text>',
        '<rect x="1815" y="28" width="525" height="66" rx="16" fill="#fff7ed" stroke="#fb923c" stroke-width="2"/>',
        '<text x="1838" y="55" class="box-title" fill="#9a3412">源码已由 Codex 薄入口接入</text>',
        '<text x="1838" y="80" class="small">插件市场包未安装 · 真实案件回放仍待完成</text>',
    ]

    parts.append(section(50, 120, 2300, 125, "律师责任层 · AI 只形成候选，批准绑定精确对象、版本与范围", "#fbf7ff", "#d8b4fe"))
    gates = [
        (82, "G1 策略", "主线、请求/抗辩"),
        (455, "G2 证据", "当前提交/备用/内部"),
        (828, "G3 提纲", "结构与篇幅预算"),
        (1201, "MEMORY", "候选→案件正式记录"),
        (1574, "G4 包快照", "精确版本与包哈希"),
        (1947, "G5 外部动作", "本版本执行数量为 0"),
    ]
    for x, title, subtitle in gates:
        parts.extend(
            [
                f'<rect x="{x}" y="172" width="335" height="54" rx="12" fill="#f3e8ff" stroke="#d8b4fe"/>',
                f'<text x="{x + 16}" y="194" class="gate-title">{escape(title)}</text>',
                f'<text x="{x + 16}" y="216" class="gate-sub">{escape(subtitle)}</text>',
            ]
        )

    parts.append(section(50, 270, 2300, 300, "① 自然语言编译与调度 · 当前原话只生成本轮控制对象，不成为案件事实"))
    parts.extend(
        [
            box(80, 335, 300, 185, "多平台对话入口", ["GPT / Claude / 千问等宿主", "当前用户命令", "材料、引文与无引号正文", "模板参数与文件定位", "对话窗口只是临时工作台"], "#ffffff", "#94a3b8"),
            box(420, 320, 350, 215, "分段 + TaskFrame", ["命令平面与数据平面隔离", "任务、对象、阶段、范围", "read ACL 与 mutation scope", "证据来源与字段 provenance", "复杂度和展示长度分离", "StylePatch 仅影响本轮"], "#e0f2fe", "#38bdf8"),
            box(810, 335, 280, 185, "单问澄清门", ["先判断能否安全继续", "最多询问一个关键问题", "R3 外部动作提前阻断", "缺范围/来源时停止", "其余歧义使用显式假设"], "#fff7ed", "#fb923c"),
            box(1130, 320, 320, 215, "RunSpec", ["Quick / Standard / Deep / Discuss", "锁定输入、产物、工具和预算", "验证器、停止条件和复核点", "展示简短不降低法律门禁", "外部动作默认不允许", "偏好自动学习固定关闭"], "#eef2ff", "#818cf8"),
            box(1490, 320, 390, 215, "IntentEnvelope / FocusContext", ["记忆读：off / file_scoped", "　　　　relevant / reflect", "记忆写：none / candidate / commit", "当前争点、材料与状态版本", "范围与读取权限分别冻结", "命令、引文和正文保持隔离"], "#ecfeff", "#06b6d4"),
            box(1920, 320, 395, 215, "薄型总调度", ["读取状态 → 选择一个专业工位", "校验返回 → 候选或原子回写", "门禁、缺项、失败或完成时停", "‘ok’只认唯一未变化对象", "R3 在工具分配前阻断", "不在总控中重做专业判断"], "#2563eb", "#1d4ed8", title_fill="#ffffff", body_fill="#e8f1ff", rx=20),
            arrow(380, 427, 420, 427, "flow-blue"),
            arrow(770, 427, 810, 427, "flow-blue"),
            arrow(1090, 427, 1130, 427, "flow-blue"),
            arrow(1450, 427, 1490, 427, "flow-blue"),
            arrow(1880, 427, 1920, 427, "flow-blue"),
        ]
    )

    parts.append(section(50, 595, 2300, 360, "② 案件内状态与有限记忆 · 唯一真值在案件文件夹，聊天和全局产品记忆不承接案情", "#fffdf8", "#fdba74"))
    parts.extend(
        [
            box(80, 665, 460, 235, "规范状态与来源链", ["case-state.json：唯一当前投影", "CaseEvent + audit-log：追加历史", "Source：原件路径、哈希与版本", "Current Case View：可重建短视图", "Context Capsule：本轮临时上下文", "00-originals 永远只读；派生件分层"], "#ecfeff", "#06b6d4"),
            box(590, 680, 360, 205, "四种读取模式", ["off：无案件时默认关闭", "file_scoped：只沿指定来源关系", "relevant：按任务有限召回", "reflect：触发式跨批次反思", "读取永远不授权写入"], "#e0f2fe", "#38bdf8"),
            box(1000, 680, 360, 205, "三种写入模式", ["none：不形成案件变更", "candidate：只返回待审 delta", "commit：版本和批准满足后写回", "候选不等于事实/证据/策略", "MemoryDelta 必须可追溯"], "#f3e8ff", "#a855f7"),
            box(1410, 680, 400, 205, "增量与生命周期对象", ["MaterialBatch / TriageCard", "Deadline / Impact / Prospective Seed", "WorkflowInstance：跨窗口持久工单", "RunAttempt：可失败、可重试运行", "局部失效后复用原有专业工位"], "#fff7ed", "#fb923c"),
            box(1860, 665, 455, 235, "多窗口与恢复", ["state_version 拒绝已过期窗口", "reconcile 只读检查哈希与游标", "等待事项不依赖聊天窗口存活", "state 与 audit 中断可被发现", "跨进程文件锁 + CAS 阻断并发覆盖", "不做实时全盘扫描或全局长记忆"], "#fff1f2", "#fb7185"),
            arrow(540, 782, 590, 782, "read"),
            arrow(950, 782, 1000, 782, "control"),
            arrow(1360, 782, 1410, 782, "flow-orange"),
            arrow(1810, 782, 1860, 782, "flow-blue"),
            poly([(2118, 535), (2118, 620), (2088, 620), (2088, 665)], "read"),
            pill(750, 908, 900, "记忆、期限、影响与工单均绑定来源、状态版本和人工门禁", "#cffafe"),
        ]
    )

    parts.append(section(50, 980, 2300, 360, "③ 个人模板学习与文书合成 · 原模板只读，学习画像、内部控制记录与正式文书分离"))
    parts.extend(
        [
            box(80, 1050, 420, 235, "14 个公开 Skill", ["含模板总控与个人模板学习", "总控 / 预处理 / 入库 / 生命周期", "案件分析 / 证据门禁 / 法源核验", "风格范例 / 起草 / 审阅 / 压缩", "手续组卷与长期工单", "固定手续模板需本机扩展另行配置"], "#eef2ff", "#818cf8"),
            box(550, 1050, 460, 235, "模板学习与登记", ["候选原件只读 + SHA-256", "FormProfile：逐格规则与风险字段", "WritingProfile：版式、结构、语气、论证", "旧 .doc → 短路径派生 DOCX + 双哈希", "_profiles 保存画像与渲染基线", "批准前不得进入正式模板目录"], "#ecfdf5", "#22c55e"),
            box(1060, 1050, 400, 235, "简单文书 / 手续保真", ["复制完整 DOCX，不用 Markdown 重做", "稳定节点替换 + 受登记结构变换", "收费、权限等必须由律师决定", "条件不成立留空；签章区 manual_blank", "行克隆/正文块仅 TEST-ONLY 验证", "原件、页眉页脚和表格几何不动"], "#fff7ed", "#fb923c"),
            box(1510, 1050, 390, 235, "复杂文书 CompositionSpec", ["一份版式主模板 + 一份结构主范例", "最多三份分段辅助范例", "当前事实/证据/法源优先于旧稿", "逐命题 ClaimBinding + 不利法源处理", "citation audit + exemplar leak check", "上游变化自动使计划和成果失效"], "#e0f2fe", "#38bdf8"),
            box(1950, 1050, 365, 235, "输出与完成边界", ["简单案可推进至 G4 候选包", "复杂争点单独深析后再合成", "独立审阅 → 授权清洁 → 再审", "未批准证据/未核验法源为 0", "无网络/API 时明确降级或停止", "打印/发送/上传/提交为 0"], "#f3e8ff", "#a855f7"),
            arrow(500, 1168, 550, 1168, "flow-blue"),
            arrow(1010, 1168, 1060, 1168, "flow-orange"),
            arrow(1460, 1168, 1510, 1168, "flow-blue"),
            arrow(1900, 1168, 1950, 1168, "flow-blue"),
        ]
    )

    parts.append(section(50, 1365, 2300, 180, "共享底座与离线证明", "#fffaf2", "#fdba74"))
    parts.extend(
        [
            box(80, 1417, 510, 95, "39 个确定性 CLI 命令", ["画像、填充、结构测试、合成、引用和防串案审计", "机械结果可复核；法律判断仍由律师负责"], "#ffffff", "#fdba74"),
            box(630, 1417, 510, 95, "统一 Schema 与政策", ["TaskFrame / RunSpec / case-state / audit", "来源、批准、失效、原件与外部动作边界"], "#ffffff", "#fdba74"),
            box(1180, 1417, 510, 95, "模板、画像与案件目录", ["待登记 / 简单 / 复杂 / 手续 / 停用 + _profiles", "画像不迁移旧案事实，模板缺失不假装套用"], "#ffffff", "#fdba74"),
            box(1730, 1417, 585, 95, "TEST-ONLY 离线验收", ["全量单测 + 离线验收；结构/边界/串案/引文均有回归", "原件哈希不变 · 正式法源门禁 · G5 外部动作 0"], "#ffffff", "#fdba74"),
        ]
    )

    parts.append(section(50, 1570, 2300, 140, "冻结前验证重点", "#fff7ed", "#fb923c"))
    parts.extend(
        [
            pill(80, 1625, 520, "首批 FormProfile / WritingProfile 人工校准", "#ffedd5", "#9a3412"),
            pill(640, 1625, 520, "最长名称、大额金额与空值边界渲染", "#ffedd5", "#9a3412"),
            pill(1200, 1625, 520, "真实案件组合回放与法源逐命题核验", "#ffedd5", "#9a3412"),
            pill(1760, 1625, 555, "律师批准模板后再激活；系统不替代递交前审阅", "#ffedd5", "#9a3412"),
            '<text x="60" y="1740" class="small">蓝色实线：生产/调度流　橙色虚线：增量与局部影响　青色虚线：按需读取　紫色虚线：人工门禁　长期状态只存在于单案目录</text>',
        ]
    )

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "architecture.svg")
    args = parser.parse_args()
    args.output.write_text(build_svg(), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
