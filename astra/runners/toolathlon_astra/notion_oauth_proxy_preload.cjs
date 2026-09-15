'use strict';

// Loaded only by the opt-in Notion authorization wrapper.
const path = require('node:path');
const source = process.env.TOOLATHLON_SOURCE || '/home/vagrant/dataset/Toolathlon';
const { ProxyAgent, setGlobalDispatcher } = require(path.join(source, 'node_modules/undici'));
const uri = process.env.TOOLATHLON_NOTION_PROXY || 'http://127.0.0.1:7890';
setGlobalDispatcher(new ProxyAgent({
  uri,
  requestTls: { rejectUnauthorized: true },
  proxyTls: { rejectUnauthorized: true },
}));
