// Vercel Serverless Function
// 测试：能否正常返回中文

module.exports = async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.status(200).send(JSON.stringify({
    status: 'ok',
    message: '服务器正常，中文测试：贵州茅台、宁德时代',
    time: Date.now(),
  }));
};
