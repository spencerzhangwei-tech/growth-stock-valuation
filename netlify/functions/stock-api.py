"""
Netlify Function: 成长股估值分析
基于 ROE + PS + PEG 三维估值体系
触发：GET /.netlify/functions/stock-api?code=sh600519
"""

import json
import re
import math
import urllib.parse
import concurrent.futures
import requests

# ─── 工具函数 ────────────────────────────────────────────

def float_or(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def fetch_tencent_quote(code):
    """获取实时行情（腾讯接口，支持CORS）"""
    if code.startswith('sh') or code.startswith('sz'):
        tc_code = code
    elif re.match(r'^\d{6}$', code):
        tc_code = ('sz' + code) if code.startswith(('0', '3')) else ('sh' + code)
    else:
        return None

    url = f"https://qt.gtimg.cn/q={tc_code}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Referer': 'https://finance.qq.com'
    }

    try:
        resp = requests.get(url, headers=headers, timeout=8)
        raw = resp.content
        try:
            text = raw.decode('gbk')
        except Exception:
            text = raw.decode('latin1')

        m = re.search(r'v_[a-z]{2}\d{6}="([^"]+)"', text)
        if not m:
            return None

        fields = m.group(1).split('~')
        if len(fields) < 45:
            return None

        name = fields[1] if len(fields) > 1 else ''
        price = float_or(fields[3], 0)
        yesterday_close = float_or(fields[4], price)
        change_pct = ((price - yesterday_close) / yesterday_close * 100) if yesterday_close else 0
        market_cap = float_or(fields[44], 0)   # 亿
        flow_cap = float_or(fields[45], 0)      # 亿
        pe_raw = float_or(fields[39], 0) if len(fields) > 39 else 0
        pe = pe_raw if 0 < pe_raw < 500 else None
        pb_raw = float_or(fields[46], 0) if len(fields) > 46 else 0
        pb = pb_raw if 0 < pb_raw < 100 else None
        exchange = '深交所' if tc_code.startswith('sz') else '上交所'

        return {
            'name': name, 'code': code, 'price': price,
            'change_pct': round(change_pct, 2),
            'market_cap': round(market_cap, 2),
            'flow_cap': round(flow_cap, 2),
            'pb': round(pb, 2) if pb else None,
            'pe': round(pe, 2) if pe else None,
            'exchange': exchange,
            'yesterday_close': yesterday_close
        }
    except Exception as e:
        print(f"Quote error: {e}")
        return None


def fetch_financial_data_em(code):
    """获取财务数据（东方财富接口）"""
    if code.startswith('sh') or code.startswith('sz'):
        em_code = code.replace('sh', 'SH').replace('sz', 'SZ')
    elif re.match(r'^\d{6}$', code):
        em_code = ('SZ' + code) if code.startswith(('0', '3')) else ('SH' + code)
    else:
        return None

    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew?type=0&code={em_code}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Referer': 'https://emweb.securities.eastmoney.com'
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        records = data.get('data', [])
        if not records:
            return None

        # 取年报数据
        annual = None
        for r in records:
            if '-12-31' in r.get('REPORT_DATE', ''):
                annual = r
                break
        latest = annual if annual else records[0]

        # 前一年年报
        prev = None
        if annual:
            for r in records:
                if r is not annual and '-12-31' in r.get('REPORT_DATE', ''):
                    prev = r
                    break
        if not prev and len(records) > 1:
            prev = records[1] if records[0] is not records[1] else (records[2] if len(records) > 2 else None)

        def get_val(r, key, div=1):
            v = r.get(key) if r else None
            return round(v / div, 2) if v else 0

        revenue = get_val(latest, 'TOTALOPERATEREVE', 1e8)
        net_profit = get_val(latest, 'PARENTNETPROFIT', 1e8)
        eps = round(latest.get('EPSJB', 0) or 0, 2)
        bps = round(latest.get('BPS', 0) or 0, 2)

        prev_rev = get_val(prev, 'TOTALOPERATEREVE', 1e8) if prev else 0
        prev_profit = get_val(prev, 'PARENTNETPROFIT', 1e8) if prev else 0

        # 增速
        rev_growth = round(latest.get('TOTALOPERATEREVETZ', 0) or 0, 2)
        profit_growth = round(latest.get('PARENTNETPROFITTZ', 0) or 0, 2)
        if prev_rev > 0 and rev_growth == 0:
            rev_growth = round((revenue - prev_rev) / prev_rev * 100, 2)
        if prev_profit > 0 and profit_growth == 0:
            profit_growth = round((net_profit - prev_profit) / prev_profit * 100, 2)

        return {
            'roe': round(latest.get('ROEJQ', 0) or 0, 2),
            'rev_growth': rev_growth,
            'profit_growth': profit_growth,
            'revenue': revenue,
            'net_profit': net_profit,
            'eps': eps,
            'bps': bps,
        }
    except Exception as e:
        print(f"Financial error: {e}")
        return None


def calculate_valuation(quote, financial):
    """计算成长股估值"""
    price = quote['price']
    market_cap = quote['market_cap']
    roe = financial.get('roe', 0)
    rev_growth = financial.get('rev_growth', 0)
    profit_growth = financial.get('profit_growth', 0)
    revenue = financial.get('revenue', 0)
    eps = financial.get('eps', 0)
    negative_growth = profit_growth < 0

    # ── 1. ROE ──
    if roe >= 30:
        roe_rating = '顶级成长'; roe_pass = True; roe_score = 100
    elif roe >= 20:
        roe_rating = '优质成长'; roe_pass = True; roe_score = 75 + (roe - 20) * 2.5
    elif roe >= 15:
        roe_rating = '一般成长'; roe_pass = False; roe_score = 50 + (roe - 15) * 5
    else:
        roe_rating = '弱成长'; roe_pass = False; roe_score = max(0, roe * 3)

    # ── 2. PS ──
    ps_current = round(market_cap / revenue, 1) if revenue > 0 and market_cap > 0 else None

    if rev_growth >= 100:
        ps_low, ps_high, ps_ideal = 12, 20, 16
    elif rev_growth >= 50:
        ps_low, ps_high, ps_ideal = 8, 12, 10
    elif rev_growth >= 30:
        ps_low, ps_high, ps_ideal = 5, 8, 6.5
    else:
        ps_low, ps_high, ps_ideal = 3, 6, 4.5

    if ps_current is not None:
        if ps_current <= ps_low:
            ps_status = '低估'; ps_score = min(100, 80 + (ps_low - ps_current) / ps_low * 20)
        elif ps_current <= ps_high:
            ps_status = '合理'; ps_score = 60 + (ps_high - ps_current) / (ps_high - ps_low) * 20
        else:
            ps_status = '偏高'; ps_score = max(0, 60 - (ps_current - ps_high) / ps_high * 40)
    else:
        ps_status = '数据不足'; ps_score = 50; ps_current = None

    # ── 3. PEG ──
    pe_current = quote.get('pe')
    if pe_current and pe_current > 0 and profit_growth > 0:
        peg = pe_current / profit_growth
    else:
        peg = None

    if peg is not None:
        if peg <= 0.8:
            peg_status = '严重低估'; peg_score = 100
        elif peg <= 1.0:
            peg_status = '低估'; peg_score = 80 + (1.0 - peg) / 0.2 * 10
        elif peg <= 1.2:
            peg_status = '合理'; peg_score = 60 + (1.2 - peg) / 0.2 * 20
        elif peg <= 1.5:
            peg_status = '略高估'; peg_score = 40 + (1.5 - peg) / 0.3 * 20
        else:
            peg_status = '严重高估'; peg_score = max(0, 40 - (peg - 1.5) * 15)
    elif negative_growth:
        peg_status = '利润下滑'; peg_score = 20; peg = None
    else:
        peg_status = '数据不足'; peg_score = 50; peg = None

    # ── 4. 综合评分 ──
    total_score = roe_score * 0.30 + ps_score * 0.30 + peg_score * 0.40

    # ── 5. 合理股价 ──
    if profit_growth > 0 and eps > 0:
        fair_pe_peg = profit_growth
        fair_price_peg = fair_pe_peg * eps
        safe_price_peg = profit_growth * 0.8 * eps
    else:
        fair_pe_peg = None; fair_price_peg = None; safe_price_peg = None

    if revenue > 0 and ps_ideal and price > 0 and market_cap > 0:
        total_shares = market_cap / price
        fair_price_ps = ps_ideal * revenue / total_shares if total_shares > 0 else None
    else:
        fair_price_ps = None

    prices_peg = [p for p in [fair_price_peg, safe_price_peg] if p and p > 0]
    if prices_peg:
        fair_price = sum(prices_peg) / len(prices_peg)
        min_price = min(prices_peg)
    else:
        fair_price = fair_price_ps; min_price = None

    upside = round((fair_price - price) / price * 100, 1) if fair_price and price > 0 else None

    # ── 6. 结论 ──
    peg_str = f"{peg:.2f}" if peg is not None else "N/A"
    if total_score >= 80:
        verdict = '🌟 优质成长股，可重点关注'
        verdict_detail = f'ROE={roe}%（{roe_rating}）+ 净利增速{profit_growth}% + PEG={peg_str}，三维评分{total_score:.0f}分，估值合理或偏低。'
    elif total_score >= 60:
        verdict = '✅ 合格成长股，可择机布局'
        verdict_detail = f'ROE={roe}% + 净利增速{profit_growth}% + PEG={peg_str}，三维评分{total_score:.0f}分，需结合行业景气度判断。'
    elif total_score >= 40:
        verdict = '⚠️ 成长性一般，谨慎参与'
        verdict_detail = f'ROE={roe}%（{roe_rating}）+ 净利增速{profit_growth}%，三维评分{total_score:.0f}分。{"利润下滑中，注意风险。" if negative_growth else "高增速难以持续，注意估值风险。"}'
    else:
        verdict = '🚫 成长陷阱，回避'
        verdict_detail = f'ROE={roe}%（{roe_rating}）+ 净利增速{profit_growth}% + PEG={peg_str}，三维评分{total_score:.0f}分，ROE不足20%或PEG严重偏高。'

    # ── 7. 一句话 ──
    if negative_growth:
        one_liner = f'⚠️ 净利润增速{profit_growth}%，利润下滑中，估值方法失效，回避或观望。'
    elif roe < 20:
        one_liner = f'❌ ROE仅{roe}%，低于20%门槛，典型「成长陷阱」，回避。'
    elif peg is not None and peg > 1.5:
        one_liner = f'⚠️ PEG={peg:.1f}>1.5，估值偏高。'
    elif peg is not None and peg < 0.8:
        one_liner = f'💎 PEG={peg:.2f}<0.8，严重低估，重仓信号！'
    elif ps_current and ps_current > ps_high:
        one_liner = f'⚠️ PS={ps_current:.1f}倍，超过{rev_growth}%增速合理区间({ps_low}-{ps_high}倍)，偏高。'
    else:
        one_liner = f'✅ ROE={roe}% + 增速{profit_growth}% + PEG={peg_str}，三维评分{total_score:.0f}分，估值合理。'

    return {
        'roe': {'value': roe, 'rating': roe_rating, 'pass': roe_pass, 'score': round(roe_score, 1)},
        'ps': {'current': ps_current, 'low': ps_low, 'high': ps_high, 'ideal': ps_ideal, 'status': ps_status, 'score': round(ps_score, 1)},
        'peg': {'current': round(peg, 2) if peg else None, 'fair_pe': round(fair_pe_peg, 1) if fair_pe_peg else None, 'status': peg_status, 'score': round(peg_score, 1)},
        'fair_price': {
            'peg': round(fair_price_peg, 2) if fair_price_peg else None,
            'ps': round(fair_price_ps, 2) if fair_price_ps else None,
            'consensus': round(fair_price, 2) if fair_price else None,
            'safe': round(min_price, 2) if min_price else None,
            'upside_pct': upside,
        },
        'score': round(total_score, 1),
        'verdict': verdict,
        'verdict_detail': verdict_detail,
        'one_liner': one_liner,
    }


# ─── Netlify Function Handler ───────────────────────────────────────────

def handler(event, context):
    # CORS 头
    headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'GET, OPTIONS',
        'Content-Type': 'application/json',
    }

    # 处理 CORS preflight
    if event.get('httpMethod') == 'OPTIONS':
        return {'statusCode': 200, 'headers': headers, 'body': ''}

    params = event.get('queryStringParameters') or {}
    search_q = params.get('q', '').strip()
    code_q = params.get('code', '').strip()

    # ── 搜索接口 ──
    if search_q:
        url = f"https://smartbox.gtimg.cn/s3/?v=1&t=all&q={urllib.parse.quote(search_q)}"
        hdrs_req = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com'}
        try:
            resp = requests.get(url, headers=hdrs_req, timeout=5)
            text = resp.content.decode('utf-8', errors='replace')
            results = []
            if 'v_hint=' in text:
                for line in text.split('\n'):
                    if 'v_hint="N"' not in line and 'v_hint=' in line:
                        m = re.search(r'v_hint="([^"]+)"', line)
                        if m:
                            parts = m.group(1).split('~')
                            if len(parts) >= 3 and parts[0] in ('sz', 'sh'):
                                try:
                                    name_decoded = parts[2].encode('utf-8').decode('unicode_escape')
                                except Exception:
                                    name_decoded = parts[2]
                                results.append({
                                    'name': name_decoded,
                                    'code': parts[0] + parts[1],
                                    'exchange': '深交所' if parts[0] == 'sz' else '上交所',
                                })
            body = json.dumps(results[:8], ensure_ascii=False)
            return {'statusCode': 200, 'headers': headers, 'body': body}
        except Exception as e:
            return {'statusCode': 500, 'headers': headers, 'body': json.dumps({'error': str(e)})}

    # ── 分析接口 ──
    if not code_q:
        return {'statusCode': 400, 'headers': headers, 'body': json.dumps({'error': '请输入股票代码或名称'})}

    # 解析代码
    def resolve_code(q):
        q = q.strip()
        if re.match(r'^\d{6}$', q):
            return ('sz' + q) if q.startswith(('0', '3')) else ('sh' + q)
        if q.startswith('sh') or q.startswith('sz'):
            return q
        # 中文名搜索
        url = f"https://smartbox.gtimg.cn/s3/?v=1&t=all&q={urllib.parse.quote(q)}"
        hdrs_req = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com'}
        try:
            resp = requests.get(url, headers=hdrs_req, timeout=5)
            text = resp.content.decode('utf-8', errors='replace')
            for line in text.split('\n'):
                if 'v_hint="N"' not in line and 'v_hint=' in line:
                    m = re.search(r'v_hint="([^"]+)"', line)
                    if m:
                        parts = m.group(1).split('~')
                        if len(parts) >= 3 and parts[0] in ('sz', 'sh'):
                            return parts[0] + parts[1]
        except Exception:
            pass
        return None

    code = resolve_code(code_q)
    if not code:
        return {'statusCode': 400, 'headers': headers, 'body': json.dumps({'error': f'无法识别股票: {code_q}，请输入6位代码'})}

    # 并行获取数据
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(fetch_tencent_quote, code)
        f2 = executor.submit(fetch_financial_data_em, code)
        quote = f1.result()
        financial = f2.result()

    if not quote:
        return {'statusCode': 400, 'headers': headers, 'body': json.dumps({'error': f'获取股票行情失败: {code}'})}
    if not financial:
        return {'statusCode': 400, 'headers': headers, 'body': json.dumps({'error': f'获取财务数据失败: {code}'})}

    valuation = calculate_valuation(quote, financial)
    body = json.dumps({'stock': quote, 'financial': financial, 'valuation': valuation}, ensure_ascii=False)
    return {'statusCode': 200, 'headers': headers, 'body': body}
