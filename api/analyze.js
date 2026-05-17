module.exports = async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.status(200).send(JSON.stringify({ message: 'ok', time: Date.now() }));
};
