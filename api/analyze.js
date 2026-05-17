/**
 * Vercel Serverless Function: 成长股估值分析
 */

const RETRY = (fn, retries = 3, delay = 2000) =>
  fn().catch(e => retries > 0 ? new Promise(r => setTimeout(r, delay)).then(() => RETRY(fn, retries - 1, delay * 1.5)) : Promise.reject(e));

// ─── 腾讯实时行情 ────────────────────────────────────────────
async function fetchTencentQuote(code) {
  const tc_code = code.startsWith('sh') || code.startsWith('sz')
    ? code
    : (code.startsWith('0') || code.startsWith('3') ? 'sz' + code : 'sh' + code);

  const url = `https://qt.gtimg.cn/q=${tc_code}`;
  let res;
  try {
    res = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com' }
    });
  } catch (e) { return null; }

  let text;
  try { text = await res.text(); } catch (e) { return null; }

  const m = text.match(/v_[a-z]{2}\d{6}="([^"]+)"/);
  if (!m) return null;

  const f = m[1].split('~');
  if (f.length < 45) return null;

  const price = parseFloat(f[3]) || 0;
  const yesterdayClose = parseFloat(f[4]) || price;
  const changePct = yesterdayClose ? ((price - yesterdayClose) / yesterdayClose * 100) : 0;
  const peRaw = parseFloat(f[39]) || 0;
  const pbRaw = parseFloat(f[46]) || 0;

  return {
    name: f[1] || '',
    code,
    price,
    change_pct: Math.round(changePct * 100) / 100,
    volume: parseInt(f[6]) || 0,
    market_cap: Math.round((parseFloat(f[44]) || 0) * 100) / 100,
    flow_cap: Math.round((parseFloat(f[45]) || 0) * 100) / 100,
    pb: (0 < pbRaw && pbRaw < 100) ? Math.round(pbRaw * 100) / 100 : null,
    pe: (0 < peRaw && peRaw < 500) ? Math.round(peRaw * 100) / 100 : null,
    exchange: tc_code.startsWith('sz') ? '深交所' : '上交所',
    yesterday_close: yesterdayClose,
  };
}

// ─── 腾讯股票搜索 ────────────────────────────────────────────
async function searchStocks(query) {
  const url = `https://smartbox.gtimg.cn/s3/?v=1&t=all&q=${encodeURIComponent(query)}`;
  let res;
  try {
    res = await fetch(url, {
      headers: { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.qq.com' }
    });
  } catch (e) { return []; }

  let text;
  try { text = await res.text(); } catch (e) { return []; }

  const results = [];
  for (const line of text.split('\n')) {
    if (line.includes('v_hint=') && !line.includes('v_hint="N"')) {
      const m = line.match(/v_hint="([^"]+)"/);
      if (!m) continue;
      const p = m[1].split('~');
      if (p.length < 3 || !['sz', 'sh'].includes(p[0])) continue;
      let name = p[2];
      try { name = JSON.parse(`"${name}"`); } catch (_) {}
      results.push({
        name,
        code: p[0] + p[1],
        exchange: p[0] === 'sz' ? '深交所' : '上交所',
      });
    }
  }
  return results.slice(0, 8);
}

// ─── 东方财富财务数据 ──────────────────────────────────────
async function fetchFinancialData(code) {
  const em_code = code.startsWith('sh') || code.startsWith('sz')
    ? code.replace('sh', 'SH').replace('sz', 'SZ')
    : (code.startsWith('0') || code.startsWith('3') ? 'SZ' + code : 'SH' + code);

  const url = `https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew?type=0&code=${em_code}`;

  let res;
  try {
    res = await RETRY(() =>
      fetch(url, {
        headers: { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://emweb.securities.eastmoney.com' }
      })
    );
  } catch (e) { return null; }

  let data;
  try { data = await res.json(); } catch (e) { return null; }

  const records = data?.data || [];
  if (!records.length) return null;

  let annual = records.find(r => r.REPORT_DATE?.includes('-12-31'));
  let latest = annual || records[0];
  let prev = records[1] || null;

  const roe = latest.ROEJQ || 0;
  const revGrowth = latest.TOTALOPERATEREVETZ || 0;
  const profitGrowth = latest.PARENTNETPROFITTZ || 0;
  const revenue = (latest.TOTALOPERATEREVE || 0) / 1e8;
  const netProfit = (latest.PARENTNETPROFIT || 0) / 1e8;
  const eps = latest.EPSJB || 0;
  const bps = latest.BPS || 0;

  const prevRevenue = prev ? (prev.TOTALOPERATEREVE || 0) / 1e8 : revenue / (1 + revGrowth / 100) || revenue;
  const prevProfit = prev ? (prev.PARENTNETPROFIT || 0) / 1e8 : netProfit / (1 + profitGrowth / 100) || netProfit;
  const prevRoe = prev ? (prev.ROEJQ || 0) : roe;

  return {
    roe: Math.round(roe * 100) / 100,
    rev_growth: Math.round(revGrowth * 100) / 100,
    profit_growth: Math.round(profitGrowth * 100) / 100,
    revenue: Math.round(revenue * 100) / 100,
    net_profit: Math.round(netProfit * 100) / 100,
    eps: Math.round(eps * 100) / 100,
    bps: Math.round(bps * 100) / 100,
    prev_roe: Math.round(prevRoe * 100) / 100,
    prev_revenue: Math.round(prevRevenue * 100) / 100,
    prev_net_profit: Math.round(prevProfit * 100) / 100,
  };
}

// ─── 估值计算 ──────────────────────────────────────────────
function calculateValuation(quote, financial) {
  const { price, market_cap: marketCap } = quote;
  const { roe, rev_growth: revGrowth, profit_growth: profitGrowth, revenue, eps } = financial;

  let roeRating, roePass, roeScore;
  if (roe >= 30) { roeRating = '顶级成长'; roePass = true; roeScore = 100; }
  else if (roe >= 20) { roeRating = '优质成长'; roePass = true; roeScore = 75 + (roe - 20) * 2.5; }
  else if (roe >= 15) { roeRating = '一般成长'; roePass = false; roeScore = 50 + (roe - 15) * 5; }
  else { roeRating = '弱成长'; roePass = false; roeScore = Math.max(0, roe * 3); }

  let psCurrent = (revenue > 0 && marketCap > 0) ? marketCap / revenue : null;
  let psLow, psHigh, psIdeal, psStatus, psScore;
  if (revGrowth >= 100) { psLow = 12; psHigh = 20; psIdeal = 16; }
  else if (revGrowth >= 50) { psLow = 8; psHigh = 12; psIdeal = 10; }
  else if (revGrowth >= 30) { psLow = 5; psHigh = 8; psIdeal = 6.5; }
  else { psLow = 3; psHigh = 6; psIdeal = 4.5; }

  if (psCurrent != null) {
    if (psCurrent <= psLow) { psStatus = '低估'; psScore = Math.min(100, 80 + (psLow - psCurrent) / psLow * 20); }
    else if (psCurrent <= psHigh) { psStatus = '合理'; psScore = 60 + (psHigh - psCurrent) / (psHigh - psLow) * 20; }
    else { psStatus = '偏高'; psScore = Math.max(0, 60 - (psCurrent - psHigh) / psHigh * 40); }
  } else { psStatus = '数据不足'; psScore = 50; }

  const pe = quote.pe;
  const negativeGrowth = profitGrowth < 0;
  let peg = null;
  if (pe && pe > 0 && profitGrowth > 0) peg = pe / profitGrowth;

  let pegStatus, pegScore;
  if (peg != null) {
    if (peg <= 0.8) { pegStatus = '严重低估'; pegScore = 100; }
    else if (peg <= 1.0) { pegStatus = '低估'; pegScore = 80 + (1.0 - peg) / 0.2 * 10; }
    else if (peg <= 1.2) { pegStatus = '合理'; pegScore = 60 + (1.2 - peg) / 0.2 * 20; }
    else if (peg <= 1.5) { pegStatus = '略高估'; pegScore = 40 + (1.5 - peg) / 0.3 * 20; }
    else { pegStatus = '严重高估'; pegScore = Math.max(0, 40 - (peg - 1.5) * 15); }
  } else if (negativeGrowth) { pegStatus = '利润下滑'; pegScore = 20; }
  else { pegStatus = '数据不足'; pegScore = 50; }

  const totalScore = roeScore * 0.30 + psScore * 0.30 + pegScore * 0.40;

  let fairPricePeg = null, safePricePeg = null, fairPricePs = null;
  if (profitGrowth > 0 && eps > 0) {
    fairPricePeg = profitGrowth * eps;
    safePricePeg = profitGrowth * 0.8 * eps;
  }
  if (revenue > 0 && psIdeal && price > 0 && marketCap > 0) {
    const totalShares = marketCap / price;
    fairPricePs = psIdeal * revenue / totalShares || null;
  }
  const prices = [fairPricePeg, safePricePeg].filter(p => p && p > 0);
  const fairPrice = prices.length ? prices.reduce((a, b) => a + b, 0) / prices.length : fairPricePs;
  const upside = (fairPrice && price > 0) ? (fairPrice - price) / price * 100 : null;

  const pegStr = peg != null ? peg.toFixed(2) : 'N/A';
  let verdict, verdictDetail;
  if (totalScore >= 80) { verdict = '🌟 优质成长股,可重点关注'; verdictDetail = `ROE=${roe}%(${roeRating})+ 净利增速${profitGrowth}% + PEG=${pegStr},三维评分${totalScore.toFixed(0)}分,估值合理或偏低。`; }
  else if (totalScore >= 60) { verdict = '✅ 合格成长股,可择机布局'; verdictDetail = `ROE=${roe}% + 净利增速${profitGrowth}% + PEG=${pegStr},三维评分${totalScore.toFixed(0)}分。需结合行业景气度判断。`; }
  else if (totalScore >= 40) { verdict = '⚠️ 成长性一般,谨慎参与'; verdictDetail = `ROE=${roe}%(${roeRating})+ 净利增速${profitGrowth}%,三维评分${totalScore.toFixed(0)}分。${negativeGrowth ? '利润下滑中,注意风险。' : '高增速难以持续,注意估值风险。'}`; }
  else { verdict = '🚫 成长陷阱,回避'; verdictDetail = `ROE=${roe}%(${roeRating})+ 净利增速${profitGrowth}% + PEG=${pegStr},三维评分${totalScore.toFixed(0)}分。ROE不足20%或PEG严重偏高,属于典型成长陷阱。`; }

  let oneLiner;
  if (negativeGrowth) oneLiner = `⚠️ 净利润增速${profitGrowth}%,利润下滑中,估值方法失效,回避或观望。`;
  else if (roe < 20) oneLiner = `❌ ROE仅${roe}%,低于20%门槛,高增速不可持续,典型「成长陷阱」,回避。`;
  else if (peg != null && peg > 1.5) oneLiner = `⚠️ PEG=${peg.toFixed(1)}>1.5,增速(${profitGrowth}%)难支撑PE(${pe}),估值偏高。`;
  else if (peg != null && peg < 0.8) oneLiner = `💎 PEG=${peg.toFixed(2)}<0.8,增速${profitGrowth}%远超PE${pe},严重低估,重仓信号!`;
  else if (psCurrent != null && psCurrent > psHigh) oneLiner = `⚠️ PS=${psCurrent.toFixed(1)}倍,超过${revGrowth}%增速合理区间(${psLow}-${psHigh}倍),偏高。`;
  else oneLiner = `✅ ROE=${roe}% + 增速${profitGrowth}% + PEG=${pegStr},三维评分${totalScore.toFixed(0)}分,估值合理。`;

  return {
    roe: { value: roe, rating: roeRating, pass: roePass, score: Math.round(roeScore * 10) / 10 },
    ps: { current: psCurrent != null ? Math.round(psCurrent * 10) / 10 : null, low: psLow, high: psHigh, ideal: psIdeal, status: psStatus, score: Math.round(psScore * 10) / 10 },
    peg: { current: peg != null ? Math.round(peg * 100) / 100 : null, fair_pe: fairPricePeg ? Math.round(fairPricePeg * 100) / 100 : null, status: pegStatus, score: Math.round(pegScore * 10) / 10 },
    fair_price: {
      peg: fairPricePeg ? Math.round(fairPricePeg * 100) / 100 : null,
      ps: fairPricePs ? Math.round(fairPricePs * 100) / 100 : null,
      consensus: fairPrice ? Math.round(fairPrice * 100) / 100 : null,
      safe: safePricePeg ? Math.round(safePricePeg * 100) / 100 : null,
      upside_pct: upside ? Math.round(upside * 10) / 10 : null,
    },
    score: Math.round(totalScore * 10) / 10,
    verdict, verdictDetail, one_liner: oneLiner,
  };
}

// ─── 股票代码解析 ────────────────────────────────────────────
async function resolveCode(query) {
  if (/^\d{6}$/.test(query)) {
    return query.startsWith('0') || query.startsWith('3') ? 'sz' + query : 'sh' + query;
  }
  if (query.startsWith('sh') || query.startsWith('sz')) return query;
  try {
    const results = await searchStocks(query);
    return results.length > 0 ? results[0].code : null;
  } catch (e) { return null; }
}

// ─── Vercel Handler ────────────────────────────────────────
module.exports = async (req, res) => {
  try {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    if (req.method === 'OPTIONS') return res.status(200).end();

    const { code, q } = req.query || {};

    if (q != null) {
      const results = await searchStocks(String(q).trim());
      return res.status(200).type('application/json; charset=utf-8').send(JSON.stringify(results));
    }

    if (!code) {
      return res.status(400).type('application/json; charset=utf-8').send(JSON.stringify({ error: '请输入股票代码或名称' }));
    }

    const resolvedCode = await resolveCode(String(code));
    if (!resolvedCode) {
      return res.status(404).type('application/json; charset=utf-8').send(JSON.stringify({ error: `无法识别股票: ${code}` }));
    }

    const [quote, financial] = await Promise.all([
      fetchTencentQuote(resolvedCode),
      fetchFinancialData(resolvedCode),
    ]);

    if (!quote) {
      return res.status(404).type('application/json; charset=utf-8').send(JSON.stringify({ error: `获取股票行情失败: ${resolvedCode}` }));
    }
    if (!financial) {
      return res.status(404).type('application/json; charset=utf-8').send(JSON.stringify({ error: `获取财务数据失败: ${resolvedCode}` }));
    }

    const valuation = calculateValuation(quote, financial);
    return res.status(200).type('application/json; charset=utf-8').send(JSON.stringify({ stock: quote, financial, valuation }));
  } catch (e) {
    console.error('Unhandled error:', e);
    return res.status(500).type('text/plain; charset=utf-8').send('Internal error: ' + e.message);
  }
};
