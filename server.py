"""
成长股估值分析服务器
基于 ROE + PS + PEG 三维估值体系
"""

import re
import json
import time
import math
import requests
from flask import Flask, request, jsonify, send_file
from urllib.parse import quote

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False


def to_json(obj):
    """返回正确UTF-8编码的JSON（确保中文不转义）"""
    return json.dumps(obj, ensure_ascii=False)

# ─── 工具函数 ────────────────────────────────────────────

def fetch_tencent_quote(code):
    """获取实时行情(腾讯接口)"""
    # 转换代码格式
    if code.startswith('sh') or code.startswith('sz'):
        tc_code = code
    elif re.match(r'^\d{6}$', code):
        if code.startswith(('0', '3')):
            tc_code = 'sz' + code
        elif code.startswith(('6',)):
            tc_code = 'sh' + code
        else:
            tc_code = 'sh' + code
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

        # 手动处理 GBK 解码
        try:
            text = raw.decode('gbk')
        except Exception:
            text = raw.decode('latin1')

        # 解析 v_sh600519 格式
        m = re.search(r'v_[a-z]{2}\d{6}="([^"]+)"', text)
        if not m:
            return None

        fields = m.group(1).split('~')
        if len(fields) < 45:
            return None

        # 字段位置(腾讯行情格式,实测验证):
        # [1]=股票名称 [2]=代码 [3]=当前价 [4]=昨收 [5]=今开
        # [6]=成交量(手) [44]=总市值(亿) [45]=流通市值(亿)
        # [47]=营业收入(亿) [46]=PB [39]=PE(TTM)
        # [33]=52周最高 [34]=52周最低

        name = fields[1] if len(fields) > 1 else ''
        price = float_or(fields[3], 0)
        yesterday_close = float_or(fields[4], price)
        change_pct = ((price - yesterday_close) / yesterday_close * 100) if yesterday_close else 0
        volume = int(float_or(fields[6], 0))
        market_cap = float_or(fields[44], 0)  # 亿
        flow_cap = float_or(fields[45], 0)    # 亿
        revenue = float_or(fields[47], 0)     # 亿(从腾讯行情)

        # PE 和 PB 位置(确保值合理)
        pe_raw = float_or(fields[39], 0) if len(fields) > 39 else 0
        pe = pe_raw if 0 < pe_raw < 500 else None
        pb_raw = float_or(fields[46], 0) if len(fields) > 46 else 0
        pb = pb_raw if 0 < pb_raw < 100 else None

        exchange = '深交所' if tc_code.startswith('sz') else '上交所'

        return {
            'name': name,
            'code': code,
            'price': price,
            'change_pct': round(change_pct, 2),
            'volume': volume,
            'market_cap': round(market_cap, 2),
            'flow_cap': round(flow_cap, 2),
            'pb': round(pb, 2) if pb else None,
            'pe': round(pe, 2) if pe else None,
            'exchange': exchange,
            'yesterday_close': yesterday_close
        }
    except Exception as e:
        print(f"Quote fetch error: {e}")
        return None


def float_or(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def fetch_financial_data_em(code):
    """获取财务数据(东方财富接口)"""
    # 转换代码
    if code.startswith('sh') or code.startswith('sz'):
        em_code = code.replace('sh', 'SH').replace('sz', 'SZ')
    elif re.match(r'^\d{6}$', code):
        if code.startswith(('0', '3')):
            em_code = 'SZ' + code
        else:
            em_code = 'SH' + code
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
        if not records or len(records) == 0:
            return None

        # 优先取年报数据(12-31日期的),否则取最新一期
        annual = None
        for r in records:
            report_date = r.get('REPORT_DATE', '')
            if '-12-31' in report_date:
                annual = r
                break

        latest = annual if annual else records[0]
        prev = None
        # 找前一年年报
        if annual:
            for r in records:
                if r is not annual and '-12-31' in r.get('REPORT_DATE', ''):
                    prev = r
                    break
        if not prev and len(records) > 1:
            prev = records[1] if records[0] is not records[1] else (records[2] if len(records) > 2 else None)

        # 获取前一年数据(用于计算增速)
        prev = records[1] if len(records) > 1 else None

        # ROE (加权)
        roe = latest.get('ROEJQ') or 0

        # 营收增速
        rev_growth = latest.get('TOTALOPERATEREVETZ') or 0

        # 净利润增速
        profit_growth = latest.get('PARENTNETPROFITTZ') or 0

        # 营业收入(亿元)
        revenue = (latest.get('TOTALOPERATEREVE') or 0) / 1e8

        # 归母净利润(亿元)
        net_profit = (latest.get('PARENTNETPROFIT') or 0) / 1e8

        # 净资产(亿元)
        book_value = (latest.get('JZC') or 0) / 1e8 if latest.get('JZC') else None
        if not book_value:
            bps = latest.get('BPS') or 0
            total_shares = (latest.get('TOTALOPERATEREVE') or 0) / (latest.get('TOTALOPERATEREVE') or 1)  # placeholder
            # 用总股本估算
            book_value = None

        # 每股指标
        eps = latest.get('EPSJB') or 0  # 基本每股收益
        bps = latest.get('BPS') or 0  # 每股净资产

        # 前一年数据
        if prev:
            prev_revenue = (prev.get('TOTALOPERATEREVE') or 0) / 1e8
            prev_profit = (prev.get('PARENTNETPROFIT') or 0) / 1e8
            prev_roe = prev.get('ROEJQ') or 0
        else:
            prev_revenue = revenue / (1 + rev_growth/100) if rev_growth else revenue
            prev_profit = net_profit / (1 + profit_growth/100) if profit_growth else net_profit
            prev_roe = roe

        # 总股本(亿股)从市值反推
        # 市值 = 股价 × 总股本(亿股)

        return {
            'roe': round(roe, 2),
            'rev_growth': round(rev_growth, 2),
            'profit_growth': round(profit_growth, 2),
            'revenue': round(revenue, 2),
            'net_profit': round(net_profit, 2),
            'eps': round(eps, 2),
            'bps': round(bps, 2),
            'prev_roe': round(prev_roe, 2),
            'prev_revenue': round(prev_revenue, 2),
            'prev_net_profit': round(prev_profit, 2),
        }
    except Exception as e:
        print(f"Financial data error: {e}")
        return None


def calculate_valuation(quote, financial):
    """计算成长股估值"""
    price = quote['price']
    market_cap = quote['market_cap']  # 亿元

    # 如果市值字段为0,尝试用总股本计算
    if not market_cap or market_cap == 0:
        # 尝试从其他字段获取
        market_cap = 0

    roe = financial.get('roe', 0)
    rev_growth = financial.get('rev_growth', 0)
    profit_growth = financial.get('profit_growth', 0)
    revenue = financial.get('revenue', 0)
    eps = financial.get('eps', 0)
    bps = financial.get('bps', 0)

    # ── 1. ROE 判断 ──
    if roe >= 30:
        roe_rating = '顶级成长'
        roe_pass = True
        roe_score = 100
    elif roe >= 20:
        roe_rating = '优质成长'
        roe_pass = True
        roe_score = 75 + (roe - 20) * 2.5
    elif roe >= 15:
        roe_rating = '一般成长'
        roe_pass = False
        roe_score = 50 + (roe - 15) * 5
    else:
        roe_rating = '弱成长'
        roe_pass = False
        roe_score = max(0, roe * 3)

    # ── 2. PS 判断 ──
    # 市值/营收 = PS,总股本(亿) = 市值/股价
    ps_current = None
    if revenue > 0 and price > 0:
        # 反推:market_cap(亿) / revenue(亿) = PS
        # 但 market_cap 可能不准,用 股价 × 总股本
        # 从腾讯数据获取总股本
        ps_current = None  # 待计算

    # 用营收和市值估算PS
    if revenue > 0 and market_cap > 0:
        ps_current = market_cap / revenue
    else:
        ps_current = None

    # 营收增速对应合理PS
    if rev_growth >= 100:
        ps_low, ps_high = 12, 20
        ps_ideal = 16
    elif rev_growth >= 50:
        ps_low, ps_high = 8, 12
        ps_ideal = 10
    elif rev_growth >= 30:
        ps_low, ps_high = 5, 8
        ps_ideal = 6.5
    else:
        ps_low, ps_high = 3, 6
        ps_ideal = 4.5

    # PS估值
    if ps_current:
        if ps_current <= ps_low:
            ps_status = '低估'
            ps_score = min(100, 80 + (ps_low - ps_current) / ps_low * 20)
        elif ps_current <= ps_high:
            ps_status = '合理'
            ps_score = 60 + (ps_high - ps_current) / (ps_high - ps_low) * 20
        else:
            ps_status = '偏高'
            ps_score = max(0, 60 - (ps_current - ps_high) / ps_high * 40)
    else:
        ps_status = '数据不足'
        ps_score = 50
        ps_ideal = None
        ps_low = None
        ps_high = None

    # ── 3. PEG 判断 ──
    pe_current = quote.get('pe')
    negative_growth = profit_growth < 0

    if pe_current and pe_current > 0 and profit_growth > 0:
        peg = pe_current / profit_growth
    elif pe_current and pe_current > 0 and negative_growth:
        peg = None  # 负增速,PEG无意义
    else:
        peg = None

    if peg is not None:
        if peg <= 0.8:
            peg_status = '严重低估'
            peg_score = 100
        elif peg <= 1.0:
            peg_status = '低估'
            peg_score = 80 + (1.0 - peg) / 0.2 * 10
        elif peg <= 1.2:
            peg_status = '合理'
            peg_score = 60 + (1.2 - peg) / 0.2 * 20
        elif peg <= 1.5:
            peg_status = '略高估'
            peg_score = 40 + (1.5 - peg) / 0.3 * 20
        else:
            peg_status = '严重高估'
            peg_score = max(0, 40 - (peg - 1.5) * 15)
    elif negative_growth:
        peg_status = '利润下滑'
        peg_score = 20
        peg = None
    else:
        peg_status = '数据不足'
        peg_score = 50

    # ── 4. 综合评分 ──
    # 三维加权:ROE 30%, PS 30%, PEG 40%
    total_score = roe_score * 0.30 + ps_score * 0.30 + peg_score * 0.40

    # ── 5. 合理股价估算 ──
    # 基于 PEG 的合理股价(仅在正增速时有意义)
    if profit_growth > 0 and eps > 0:
        fair_pe_peg = profit_growth  # PEG=1
        fair_price_peg = fair_pe_peg * eps
        safe_pe = profit_growth * 0.8  # PEG=0.8
        safe_price_peg = safe_pe * eps
    else:
        fair_pe_peg = None
        fair_price_peg = None
        safe_price_peg = None

    # 基于 PS 的合理股价
    if revenue > 0 and ps_ideal and price > 0 and market_cap > 0:
        # 总股本(亿) = 市值(亿) / 股价
        total_shares = market_cap / price
        # 合理股价 = 合理PS × 营收(亿) / 总股本(亿)
        fair_price_ps = ps_ideal * revenue / total_shares if total_shares > 0 else None
    else:
        fair_price_ps = None

    # 综合合理股价(仅用PEG,因为PS用于判断合理性而非绝对估值)
    prices_peg = [p for p in [fair_price_peg, safe_price_peg] if p and p > 0]
    if prices_peg:
        fair_price = sum(prices_peg) / len(prices_peg)
        min_price = min(prices_peg)
    else:
        fair_price = fair_price_ps
        min_price = None

    # 当前股价偏离度
    if fair_price and price > 0:
        upside = (fair_price - price) / price * 100
    else:
        upside = None

    # ── 6. 结论判断 ──
    peg_str = f"{peg:.2f}" if peg is not None else "N/A"
    if total_score >= 80:
        verdict = '🌟 优质成长股,可重点关注'
        verdict_detail = f'ROE={roe}%({roe_rating})+ 净利增速{profit_growth}% + PEG={peg_str},三维评分{total_score:.0f}分,估值合理或偏低。'
    elif total_score >= 60:
        verdict = '✅ 合格成长股,可择机布局'
        verdict_detail = f'ROE={roe}% + 净利增速{profit_growth}% + PEG={peg_str},三维评分{total_score:.0f}分。需结合行业景气度判断。'
    elif total_score >= 40:
        verdict = '⚠️ 成长性一般,谨慎参与'
        verdict_detail = f'ROE={roe}%({roe_rating})+ 净利增速{profit_growth}%,三维评分{total_score:.0f}分。{ "利润下滑中,注意风险。" if negative_growth else "高增速难以持续,注意估值风险。"}'
    else:
        verdict = '🚫 成长陷阱,回避'
        verdict_detail = f'ROE={roe}%({roe_rating})+ 净利增速{profit_growth}% + PEG={peg_str},三维评分{total_score:.0f}分。ROE不足20%或PEG严重偏高,属于典型成长陷阱。'

    # ── 7. 一句话结论 ──
    if negative_growth:
        one_liner = f'⚠️ 净利润增速{profit_growth}%,利润下滑中,估值方法失效,回避或观望。'
    elif roe < 20:
        one_liner = f'❌ ROE仅{roe}%,低于20%门槛,高增速不可持续,典型「成长陷阱」,回避。'
    elif peg is not None and peg > 1.5:
        one_liner = f'⚠️ PEG={peg:.1f}>1.5,增速({profit_growth}%)难支撑PE({pe_current}),估值偏高。'
    elif peg is not None and peg < 0.8:
        one_liner = f'💎 PEG={peg:.2f}<0.8,增速{profit_growth}%远超PE{pe_current},严重低估,重仓信号!'
    elif ps_current and ps_high and ps_current > ps_high:
        one_liner = f'⚠️ PS={ps_current:.1f}倍,超过{rev_growth}%增速合理区间({ps_low}-{ps_high}倍),偏高。'
    else:
        one_liner = f'✅ ROE={roe}% + 增速{profit_growth}% + PEG={peg_str},三维评分{total_score:.0f}分,估值合理。'

    return {
        'roe': {
            'value': roe,
            'rating': roe_rating,
            'pass': roe_pass,
            'score': round(roe_score, 1),
        },
        'ps': {
            'current': round(ps_current, 1) if ps_current else None,
            'low': ps_low,
            'high': ps_high,
            'ideal': ps_ideal,
            'status': ps_status,
            'score': round(ps_score, 1),
        },
        'peg': {
            'current': round(peg, 2) if peg else None,
            'fair_pe': round(fair_pe_peg, 1) if fair_pe_peg else None,
            'status': peg_status,
            'score': round(peg_score, 1),
        },
        'fair_price': {
            'peg': round(fair_price_peg, 2) if fair_price_peg else None,
            'ps': round(fair_price_ps, 2) if fair_price_ps else None,
            'consensus': round(fair_price, 2) if fair_price else None,
            'safe': round(min_price, 2) if min_price else None,
            'upside_pct': round(upside, 1) if upside else None,
        },
        'score': round(total_score, 1),
        'verdict': verdict,
        'verdict_detail': verdict_detail,
        'one_liner': one_liner,
    }


# ─── 路由 ──────────────────────────────────────────────

@app.route('/')
def index():
    return send_file('index.html')


@app.route('/api/quote')
def api_quote():
    """获取实时行情"""
    code = request.args.get('code', '').strip()
    if not code:
        return jsonify({'error': '缺少股票代码'})

    # 如果是中文名,搜索
    if not re.match(r'^\d{6}$', code) and not code.startswith(('sh', 'sz')):
        return jsonify({'error': '请输入6位股票代码'})

    quote = fetch_tencent_quote(code)
    if not quote:
        return jsonify({'error': f'获取股票行情失败: {code}'})

    return jsonify(quote)


@app.route('/api/financial')
def api_financial():
    """获取财务数据"""
    code = request.args.get('code', '').strip()
    if not code:
        return jsonify({'error': '缺少股票代码'})

    financial = fetch_financial_data_em(code)
    if not financial:
        return jsonify({'error': f'获取财务数据失败: {code}'})

    return jsonify(financial)


def resolve_stock_code(query):
    """将中文股名解析为代码，返回如 sh600519 或 sz000001"""
    query = query.strip()
    # 已经是代码格式
    if re.match(r'^\d{6}$', query):
        return ('sz' + query) if query.startswith(('0', '3')) else ('sh' + query)
    if query.startswith('sh') or query.startswith('sz'):
        return query

    # 中文名称搜索
    url = f"https://smartbox.gtimg.cn/s3/?v=1&t=all&q={quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://finance.qq.com'
    }
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        raw = resp.content
        try:
            text = raw.decode('utf-8')
        except Exception:
            text = raw.decode('gbk', errors='replace')
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


@app.route('/api/analyze')
def api_analyze():
    """综合估值分析"""
    raw_query = request.args.get('code', '').strip()
    if not raw_query:
        return jsonify({'error': '请输入股票代码或名称'})

    # 解析代码
    code = resolve_stock_code(raw_query)
    if not code:
        return jsonify({'error': f'无法识别股票: {raw_query}，请输入6位代码'})

    # 并行获取行情和财务数据
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(fetch_tencent_quote, code)
        f2 = executor.submit(fetch_financial_data_em, code)

        quote = f1.result()
        financial = f2.result()

    if not quote:
        return jsonify({'error': f'获取股票行情失败: {code}'})
    if not financial:
        return jsonify({'error': f'获取财务数据失败: {code}'})

    valuation = calculate_valuation(quote, financial)

    return jsonify({
        'stock': quote,
        'financial': financial,
        'valuation': valuation,
    })


@app.route('/api/search')
def api_search():
    """搜索股票"""
    query = request.args.get('q', '').strip()
    if not query or len(query) < 1:
        return app.response_class(response='[]', status=200, mimetype='application/json')

    # 用腾讯搜索接口
    url = f"https://smartbox.gtimg.cn/s3/?v=1&t=all&q={quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://finance.qq.com'
    }

    try:
        resp = requests.get(url, headers=headers, timeout=5)
        raw = resp.content
        try:
            text = raw.decode('utf-8')
        except Exception:
            text = raw.decode('gbk', errors='replace')

        results = []
        if 'v_hint=' in text:
            for line in text.split('\n'):
                if 'v_hint="N"' not in line and 'v_hint=' in line:
                    m = re.search(r'v_hint="([^"]+)"', line)
                    if m:
                        parts = m.group(1).split('~')
                        if len(parts) >= 3:
                            prefix = parts[0]
                            c = parts[1]
                            name = parts[2]
                            if prefix in ('sz', 'sh'):
                                exchange = '深交所' if prefix == 'sz' else '上交所'
                                code = prefix + c
                                # 腾讯返回的 \u8d35\u5dde 是字面字符串，需要 decode
                                try:
                                    name_decoded = name.encode('utf-8').decode('unicode_escape')
                                except Exception:
                                    name_decoded = name
                                results.append({
                                    'name': name_decoded,
                                    'code': code,
                                    'exchange': exchange,
                                })

        return app.response_class(
            response=to_json(results[:8]),
            status=200,
            mimetype='application/json'
        )
    except Exception as e:
        print(f"Search error: {e}")
        return app.response_class(response='[]', status=200, mimetype='application/json')


if __name__ == '__main__':
    print("🚀 成长股估值分析服务启动中...")
    print("📊 访问 http://localhost:8082")
    app.run(host='0.0.0.0', port=8082, debug=False)
