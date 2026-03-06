#!/usr/bin/env python3
"""
制裁名单筛查工具 v1.0
输入：公司/个人名称
输出：HTML筛查报告
数据源：OFAC SDN List (美国财政部)
"""

import csv
import sys
import os
from datetime import datetime
from difflib import SequenceMatcher

SDN_FILE = os.path.join(os.path.dirname(__file__), "sdn.csv")

def load_sdn(path=SDN_FILE):
    """加载OFAC SDN名单"""
    entries = []
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 4:
                entries.append({
                    'id': row[0].strip(),
                    'name': row[1].strip(),
                    'type': row[2].strip(),
                    'country': row[3].strip(),
                    'remarks': row[-1].strip() if len(row) > 4 else ''
                })
    return entries

def fuzzy_match(query, target, threshold=0.75):
    """模糊匹配，返回相似度"""
    q = query.upper().strip()
    t = target.upper().strip()
    if not q or not t or t == '-0-':
        return 0.0
    # 完全包含
    if q in t or t in q:
        return 1.0
    return SequenceMatcher(None, q, t).ratio()

def screen(query, entries, threshold=0.75, max_results=20):
    """筛查一个名称"""
    results = []
    for entry in entries:
        score = fuzzy_match(query, entry['name'], threshold)
        if score >= threshold:
            results.append({**entry, 'score': score})
        # 也检查remarks里的别名
        if 'a.k.a.' in entry['remarks'].lower():
            aka_score = fuzzy_match(query, entry['remarks'])
            if aka_score >= threshold and aka_score > score:
                results.append({**entry, 'score': aka_score, 'match_field': 'aka'})
    
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:max_results]

def risk_level(results):
    if not results:
        return "LOW", "🟢", "未发现匹配"
    top = results[0]['score']
    if top >= 0.95:
        return "CRITICAL", "🔴", "高度疑似命中，必须人工复核"
    elif top >= 0.85:
        return "HIGH", "🟠", "存在较高相似度匹配，建议人工复核"
    else:
        return "MEDIUM", "🟡", "存在模糊匹配，建议进一步核实"

def generate_html_report(query, results, output_path):
    """生成HTML筛查报告"""
    level, icon, desc = risk_level(results)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    matches_html = ""
    if results:
        for i, r in enumerate(results, 1):
            score_pct = f"{r['score']*100:.0f}%"
            bar_color = "#e74c3c" if r['score'] >= 0.95 else "#f39c12" if r['score'] >= 0.85 else "#f1c40f"
            matches_html += f"""
            <tr>
                <td>{i}</td>
                <td><strong>{r['name']}</strong></td>
                <td>{r['country']}</td>
                <td>{r['type'] if r['type'] != '-0-' else 'N/A'}</td>
                <td>
                    <div style="display:flex;align-items:center;gap:8px">
                        <div style="width:60px;height:8px;background:#eee;border-radius:4px">
                            <div style="width:{score_pct};height:8px;background:{bar_color};border-radius:4px"></div>
                        </div>
                        <span style="font-weight:600">{score_pct}</span>
                    </div>
                </td>
                <td style="font-size:12px;color:#666">{r['remarks'][:80] if r['remarks'] != '-0-' else ''}</td>
            </tr>"""
    else:
        matches_html = '<tr><td colspan="6" style="text-align:center;padding:30px;color:#27ae60">✅ 未发现匹配记录</td></tr>'

    level_colors = {
        "CRITICAL": ("#e74c3c", "#fdf0ef"),
        "HIGH": ("#f39c12", "#fef9f0"),
        "MEDIUM": ("#f1c40f", "#fefcf0"),
        "LOW": ("#27ae60", "#f0fdf4"),
    }
    lc, lbg = level_colors[level]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>制裁名单筛查报告 - {query}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background:#f5f5f5; color:#333; }}
.container {{ max-width:900px; margin:0 auto; padding:20px; }}
.header {{ background:#1a1a2e; color:white; padding:30px; border-radius:12px 12px 0 0; }}
.header h1 {{ font-size:20px; margin-bottom:8px; }}
.header .subtitle {{ color:#8892b0; font-size:13px; }}
.meta {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; padding:20px; background:white; border-bottom:1px solid #eee; }}
.meta-item {{ font-size:13px; }}
.meta-item .label {{ color:#888; margin-bottom:2px; }}
.meta-item .value {{ font-weight:600; }}
.risk-banner {{ padding:20px; background:{lbg}; border-left:4px solid {lc}; margin:0; }}
.risk-banner .level {{ font-size:18px; font-weight:700; color:{lc}; }}
.risk-banner .desc {{ font-size:13px; color:#555; margin-top:4px; }}
.results {{ background:white; padding:20px; }}
.results h2 {{ font-size:16px; margin-bottom:15px; color:#1a1a2e; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ background:#f8f9fa; padding:10px; text-align:left; font-weight:600; color:#555; border-bottom:2px solid #eee; }}
td {{ padding:10px; border-bottom:1px solid #f0f0f0; }}
tr:hover {{ background:#fafafa; }}
.footer {{ padding:20px; background:white; border-radius:0 0 12px 12px; border-top:1px solid #eee; }}
.footer p {{ font-size:11px; color:#999; line-height:1.8; }}
.disclaimer {{ background:#fff3cd; padding:15px; border-radius:8px; margin-top:15px; font-size:12px; color:#856404; }}
@media print {{ body {{ background:white; }} .container {{ max-width:100%; }} }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🛡️ 制裁名单筛查报告</h1>
        <div class="subtitle">Sanctions Screening Report — OFAC SDN List</div>
    </div>
    
    <div class="meta">
        <div class="meta-item">
            <div class="label">筛查对象</div>
            <div class="value">{query}</div>
        </div>
        <div class="meta-item">
            <div class="label">筛查时间</div>
            <div class="value">{now}</div>
        </div>
        <div class="meta-item">
            <div class="label">数据源</div>
            <div class="value">OFAC SDN List (U.S. Treasury)</div>
        </div>
        <div class="meta-item">
            <div class="label">匹配阈值</div>
            <div class="value">75% (模糊匹配)</div>
        </div>
    </div>

    <div class="risk-banner">
        <div class="level">{icon} 风险等级：{level}</div>
        <div class="desc">{desc}</div>
    </div>

    <div class="results">
        <h2>匹配结果（共 {len(results)} 条）</h2>
        <table>
            <thead>
                <tr>
                    <th>#</th>
                    <th>名称</th>
                    <th>国家/地区</th>
                    <th>类型</th>
                    <th>相似度</th>
                    <th>备注</th>
                </tr>
            </thead>
            <tbody>
                {matches_html}
            </tbody>
        </table>
    </div>

    <div class="footer">
        <div class="disclaimer">
            ⚠️ <strong>免责声明</strong>：本报告基于OFAC SDN公开名单的自动化模糊匹配，仅供初步筛查参考。
            匹配结果不代表最终合规结论。任何疑似命中均需由合规专员进行人工复核，
            并结合完整的尽职调查结果做出最终判断。本工具不替代专业合规审查。
        </div>
        <p style="margin-top:10px">
            数据来源：U.S. Department of the Treasury — Office of Foreign Assets Control (OFAC)<br>
            SDN List 下载：https://www.treasury.gov/ofac/downloads/sdn.csv<br>
            报告生成工具：AI-Powered Sanctions Screening Tool v1.0
        </p>
    </div>
</div>
</body>
</html>"""
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return output_path

def main():
    if len(sys.argv) < 2:
        print("用法: python3 screen.py <筛查名称> [输出文件]")
        print("示例: python3 screen.py 'Banco Nacional de Cuba'")
        sys.exit(1)
    
    query = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else f"report_{query.replace(' ','_')[:30]}.html"
    
    print(f"📥 加载SDN名单...")
    entries = load_sdn()
    print(f"   共 {len(entries)} 条记录")
    
    print(f"🔍 筛查: {query}")
    results = screen(query, entries)
    
    level, icon, desc = risk_level(results)
    print(f"\n{icon} 风险等级: {level}")
    print(f"   {desc}")
    print(f"   找到 {len(results)} 条匹配")
    
    if results:
        print(f"\n   Top 3:")
        for r in results[:3]:
            print(f"   - {r['name']} ({r['country']}) — {r['score']*100:.0f}%")
    
    report_path = generate_html_report(query, results, output)
    print(f"\n📄 报告已生成: {report_path}")

if __name__ == "__main__":
    main()
