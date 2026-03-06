#!/usr/bin/env python3
"""
制裁名单筛查工具 v2.0
输入：公司/个人名称（或批量CSV）
输出：HTML筛查报告 + 审计日志
数据源：OFAC SDN List (美国财政部)
"""

import csv
import sys
import os
import re
import json
import hashlib
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from rapidfuzz import fuzz
    USE_RAPIDFUZZ = True
except ImportError:
    from difflib import SequenceMatcher
    USE_RAPIDFUZZ = False
    print("⚠️ 建议安装 rapidfuzz 提升匹配精度: pip install rapidfuzz")

SDN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sdn.csv")
AUDIT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.jsonl")

# 风险阈值（行业标准三级）
THRESHOLD_ALERT = 0.90    # 直接告警
THRESHOLD_REVIEW = 0.75   # 需要人工审核
# 低于 0.75 = Clear

# 公司后缀标准化映射
SUFFIX_MAP = {
    'corp': 'corporation', 'co': 'company', 'ltd': 'limited',
    'intl': 'international', 'inc': 'incorporated', 'llc': 'limited liability company',
    'gmbh': 'gesellschaft mit beschrankter haftung', 'plc': 'public limited company',
    'ag': 'aktiengesellschaft', 'sa': 'societe anonyme',
}

_sdn_cache = None

def normalize_name(name):
    """标准化名称：去标点、统一后缀、大写"""
    if not name or name == '-0-':
        return ''
    n = name.upper().strip()
    n = re.sub(r'[^\w\s]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    # 标准化公司后缀
    words = n.split()
    normalized = []
    for w in words:
        normalized.append(SUFFIX_MAP.get(w.lower(), w))
    return ' '.join(normalized).upper()

def extract_akas(remarks):
    """从remarks字段提取所有别名"""
    if not remarks or remarks == '-0-':
        return []
    akas = re.findall(r"a\.k\.a\.?\s+'([^']+)'", remarks, re.IGNORECASE)
    akas += re.findall(r'a\.k\.a\.?\s+"([^"]+)"', remarks, re.IGNORECASE)
    akas += re.findall(r"f\.k\.a\.?\s+'([^']+)'", remarks, re.IGNORECASE)
    return [a.strip() for a in akas if a.strip()]

def advanced_match(query, target):
    """高级模糊匹配：多策略取最高分"""
    q = normalize_name(query)
    t = normalize_name(target)
    if not q or not t:
        return 0.0
    # 完全匹配
    if q == t:
        return 1.0
    # 包含关系
    if q in t or t in q:
        return 0.95

    if USE_RAPIDFUZZ:
        # 主要得分：全局匹配
        ratio = fuzz.ratio(q, t) / 100
        token_sort = fuzz.token_sort_ratio(q, t) / 100
        # 部分匹配要打折（防止短字符串误匹配）
        partial = fuzz.partial_ratio(q, t) / 100
        len_ratio = min(len(q), len(t)) / max(len(q), len(t)) if max(len(q), len(t)) > 0 else 0
        partial_adjusted = partial * (0.5 + 0.5 * len_ratio)  # 长度差越大，打折越多
        # token_set也要适度打折
        token_set = fuzz.token_set_ratio(q, t) / 100 * 0.95
        return max(ratio, token_sort, partial_adjusted, token_set)
    else:
        from difflib import SequenceMatcher
        return SequenceMatcher(None, q, t).ratio()

def check_data_freshness(path, max_age_days=7):
    """检查数据时效性"""
    if not os.path.exists(path):
        return False, 0, "文件不存在"
    mtime = os.path.getmtime(path)
    age_days = (datetime.now().timestamp() - mtime) / 86400
    if age_days > max_age_days:
        return False, age_days, f"数据已 {age_days:.0f} 天未更新"
    return True, age_days, "数据在有效期内"

def auto_update_sdn(path=SDN_FILE, force=False):
    """自动下载最新SDN名单"""
    fresh, age, msg = check_data_freshness(path)
    if fresh and not force:
        return True, msg
    
    url = "https://www.treasury.gov/ofac/downloads/sdn.csv"
    print(f"📥 正在下载最新OFAC SDN名单...")
    try:
        urllib.request.urlretrieve(url, path)
        print(f"✅ 已更新：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        return True, "已更新到最新"
    except Exception as e:
        if os.path.exists(path):
            print(f"⚠️ 下载失败（{e}），使用本地缓存（{age:.0f}天前）")
            return True, f"使用本地缓存（{age:.0f}天前）"
        return False, f"下载失败且无本地缓存: {e}"

def get_data_hash(path):
    """计算数据文件哈希（用于审计）"""
    if not os.path.exists(path):
        return "N/A"
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()[:12]

def load_sdn(path=SDN_FILE):
    """加载OFAC SDN名单（带缓存）"""
    global _sdn_cache
    if _sdn_cache is not None:
        return _sdn_cache
    
    entries = []
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 4:
                name = row[1].strip()
                remarks = row[-1].strip() if len(row) > 4 else ''
                entry = {
                    'id': row[0].strip(),
                    'name': name,
                    'type': row[2].strip(),
                    'country': row[3].strip(),
                    'remarks': remarks,
                    'akas': extract_akas(remarks),
                }
                entries.append(entry)
    _sdn_cache = entries
    return entries

def screen(query, entries, threshold=THRESHOLD_REVIEW, max_results=20):
    """筛查一个名称（去重）"""
    seen = set()
    results = []
    
    for entry in entries:
        best_score = 0
        match_field = 'name'
        
        # 匹配主名称
        score = advanced_match(query, entry['name'])
        if score > best_score:
            best_score = score
            match_field = 'name'
        
        # 匹配所有别名（过滤过短的别名）
        for aka in entry.get('akas', []):
            if len(aka) < 4:  # 忽略太短的别名
                continue
            aka_score = advanced_match(query, aka)
            if aka_score > best_score:
                best_score = aka_score
                match_field = f'aka: {aka}'
        
        if best_score >= threshold:
            entry_key = entry['id']
            if entry_key not in seen:
                seen.add(entry_key)
                results.append({
                    **entry,
                    'score': best_score,
                    'match_field': match_field,
                })
    
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:max_results]

def conclude(results):
    """三级结论"""
    if not results:
        return "CLEAR", "✅", "未发现匹配（不代表绝对安全，仍需结合其他尽职调查）"
    top = results[0]['score']
    if top >= THRESHOLD_ALERT:
        return "ALERT", "🚨", "疑似制裁实体命中，必须暂停交易并人工复核"
    elif top >= THRESHOLD_REVIEW:
        return "REVIEW", "⚠️", "存在需要人工审核的匹配，建议进一步核实"
    return "CLEAR", "✅", "未发现匹配（不代表绝对安全，仍需结合其他尽职调查）"

def write_audit_log(query, results, data_hash, data_date):
    """写入审计日志"""
    conclusion, _, desc = conclude(results)
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "data_source": "OFAC SDN List",
        "data_hash": data_hash,
        "data_date": data_date,
        "match_count": len(results),
        "top_score": f"{results[0]['score']*100:.0f}%" if results else "0%",
        "conclusion": conclusion,
        "conclusion_desc": desc,
    }
    with open(AUDIT_LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

def generate_html_report(query, results, output_path, data_hash, data_date):
    """生成HTML筛查报告"""
    conclusion, icon, desc = conclude(results)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    matches_html = ""
    if results:
        for i, r in enumerate(results, 1):
            score_pct = f"{r['score']*100:.0f}%"
            bar_color = "#e74c3c" if r['score'] >= THRESHOLD_ALERT else "#f39c12" if r['score'] >= THRESHOLD_REVIEW else "#f1c40f"
            mf = r.get('match_field', 'name')
            mf_display = f'<span style="font-size:11px;color:#888">({mf})</span>' if mf != 'name' else ''
            rmk = r['remarks'][:80] if r['remarks'] != '-0-' else ''
            matches_html += f"""
            <tr>
                <td>{i}</td>
                <td><strong>{r['name']}</strong> {mf_display}</td>
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
                <td style="font-size:12px;color:#666">{rmk}</td>
            </tr>"""
    else:
        matches_html = '<tr><td colspan="6" style="text-align:center;padding:30px;color:#27ae60">✅ 未发现匹配记录</td></tr>'

    level_colors = {
        "ALERT": ("#e74c3c", "#fdf0ef"),
        "REVIEW": ("#f39c12", "#fef9f0"),
        "CLEAR": ("#27ae60", "#f0fdf4"),
    }
    lc, lbg = level_colors.get(conclusion, ("#27ae60", "#f0fdf4"))

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
.audit {{ background:#f8f9fa; padding:15px; border-radius:8px; margin-top:15px; font-size:11px; color:#666; }}
.no-match-warning {{ background:#fff3cd; padding:12px; border-radius:8px; margin-top:10px; font-size:12px; color:#856404; }}
@media print {{ body {{ background:white; }} .container {{ max-width:100%; }} }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🛡️ 制裁名单筛查报告</h1>
        <div class="subtitle">Sanctions Screening Report — OFAC SDN List v2.0</div>
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
            <div class="label">数据版本</div>
            <div class="value">{data_date} (hash: {data_hash})</div>
        </div>
    </div>

    <div class="risk-banner">
        <div class="level">{icon} 结论：{conclusion}</div>
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
        {"" if results else '<div class="no-match-warning">⚠️ 无匹配不代表绝对安全。本工具仅覆盖OFAC SDN名单，未包含EU、UN等其他制裁名单。请结合完整尽职调查做出最终判断。</div>'}
    </div>

    <div class="footer">
        <div class="disclaimer">
            ⚠️ <strong>免责声明</strong>：本报告基于OFAC SDN公开名单的自动化模糊匹配，仅供初步筛查参考。
            匹配结果不代表最终合规结论，无匹配结果也不构成合规放行依据。
            任何疑似命中均需由合规专员进行人工复核，并结合完整的尽职调查结果做出最终判断。
            本工具不替代专业合规审查，使用者自行承担筛查结果的应用风险。
        </div>
        <div class="audit">
            <strong>审计信息</strong><br>
            筛查时间：{now}<br>
            数据源哈希：{data_hash}<br>
            数据日期：{data_date}<br>
            匹配阈值：{THRESHOLD_REVIEW*100:.0f}%（审核）/ {THRESHOLD_ALERT*100:.0f}%（告警）<br>
            匹配算法：{'RapidFuzz (token_sort + partial + token_set)' if USE_RAPIDFUZZ else 'SequenceMatcher'}<br>
            签名：________________（合规专员）&nbsp;&nbsp;&nbsp;&nbsp;日期：________________
        </div>
        <p style="margin-top:10px">
            数据来源：U.S. Department of the Treasury — Office of Foreign Assets Control (OFAC)<br>
            SDN List 下载：https://www.treasury.gov/ofac/downloads/sdn.csv<br>
            报告生成工具：OFAC Sanctions Screening Tool v2.0
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
        print("用法:")
        print("  python3 screen.py <筛查名称>              # 单个筛查")
        print("  python3 screen.py --batch <CSV文件>        # 批量筛查")
        print("  python3 screen.py --update                 # 更新SDN数据")
        print("示例:")
        print("  python3 screen.py 'Banco Nacional de Cuba'")
        sys.exit(1)
    
    # 更新命令
    if sys.argv[1] == '--update':
        auto_update_sdn(force=True)
        sys.exit(0)
    
    # 检查数据时效性
    fresh, age, msg = check_data_freshness(SDN_FILE)
    if not os.path.exists(SDN_FILE):
        print("📥 首次使用，正在下载OFAC SDN名单...")
        ok, msg = auto_update_sdn()
        if not ok:
            print(f"❌ {msg}")
            sys.exit(1)
    elif not fresh:
        print(f"⚠️ {msg}")
        print("   运行 python3 screen.py --update 更新数据")
    
    # 数据信息
    data_hash = get_data_hash(SDN_FILE)
    data_date = datetime.fromtimestamp(os.path.getmtime(SDN_FILE)).strftime("%Y-%m-%d")
    
    # 批量模式
    if sys.argv[1] == '--batch':
        if len(sys.argv) < 3:
            print("❌ 请指定CSV文件路径")
            sys.exit(1)
        batch_file = sys.argv[2]
        print(f"📥 加载SDN名单...")
        entries = load_sdn()
        print(f"   共 {len(entries)} 条记录")
        
        queries = []
        with open(batch_file, 'r', encoding='utf-8') as f:
            for line in f:
                name = line.strip()
                if name:
                    queries.append(name)
        
        print(f"📋 批量筛查 {len(queries)} 个实体\n")
        summary = {"ALERT": 0, "REVIEW": 0, "CLEAR": 0}
        
        for q in queries:
            results = screen(q, entries)
            conclusion, icon, desc = conclude(results)
            summary[conclusion] += 1
            print(f"  {icon} {q} → {conclusion} ({len(results)} 条匹配)")
            
            output = f"report_{q.replace(' ','_')[:30]}.html"
            generate_html_report(q, results, output, data_hash, data_date)
            write_audit_log(q, results, data_hash, data_date)
        
        print(f"\n📊 汇总：🚨 ALERT: {summary['ALERT']} | ⚠️ REVIEW: {summary['REVIEW']} | ✅ CLEAR: {summary['CLEAR']}")
        print(f"📄 报告已生成，审计日志已写入 {AUDIT_LOG}")
        sys.exit(0)
    
    # 单个筛查
    query = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else f"report_{query.replace(' ','_')[:30]}.html"
    
    print(f"📥 加载SDN名单...")
    entries = load_sdn()
    print(f"   共 {len(entries)} 条记录（数据版本：{data_date}，哈希：{data_hash}）")
    
    print(f"🔍 筛查: {query}")
    results = screen(query, entries)
    
    conclusion, icon, desc = conclude(results)
    print(f"\n{icon} 结论: {conclusion}")
    print(f"   {desc}")
    print(f"   找到 {len(results)} 条匹配")
    
    if results:
        print(f"\n   Top 匹配:")
        for r in results[:5]:
            mf = f" [{r.get('match_field', 'name')}]" if r.get('match_field', 'name') != 'name' else ''
            print(f"   - {r['name']} ({r['country']}) — {r['score']*100:.0f}%{mf}")
    
    report_path = generate_html_report(query, results, output, data_hash, data_date)
    write_audit_log(query, results, data_hash, data_date)
    print(f"\n📄 报告已生成: {report_path}")
    print(f"📝 审计日志已写入: {AUDIT_LOG}")

if __name__ == "__main__":
    main()
