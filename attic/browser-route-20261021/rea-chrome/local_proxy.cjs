'use strict';
const http = require('node:http');
const net = require('node:net');
const arg = (n, d='') => { const h = process.argv.find(a=>a.startsWith(`--${n}=`)); return h?h.slice(n.length+3):d; };
const UPSTREAM = arg('upstream',''); const LISTEN = Number(arg('listen','8899'));
let u = UPSTREAM; if (!/^[a-z0-9+.-]+:\/\//i.test(u)) u = 'http://'+u;
const up = new URL(u);
const UP_HOST = up.hostname, UP_PORT = Number(up.port||8080);
const UP_AUTH = (up.username||up.password) ? 'Basic '+Buffer.from(decodeURIComponent(up.username)+':'+decodeURIComponent(up.password)).toString('base64') : '';
console.log('local 127.0.0.1:'+LISTEN+' -> '+UP_HOST+':'+UP_PORT+' auth='+(UP_AUTH?'on':'off'));
const server = http.createServer((req,res)=>{
  const headers={...req.headers}; if(UP_AUTH) headers['proxy-authorization']=UP_AUTH; delete headers['proxy-connection'];
  const pr=http.request({host:UP_HOST,port:UP_PORT,method:req.method,path:req.url,headers},(pres)=>{
    res.writeHead(pres.statusCode||502,pres.headers); pres.pipe(res);
  });
  pr.on('error',(e)=>{try{res.writeHead(502);res.end('upstream '+String(e).slice(0,60));}catch(_){}});
  req.pipe(pr);
});
server.on('connect',(req,clientSocket,head)=>{
  const target=String(req.url||''); const i=target.lastIndexOf(':');
  const host=i>0?target.slice(0,i):target; const port=i>0?Number(target.slice(i+1)):443;
  const upSock=net.connect(UP_PORT,UP_HOST);
  let done=false;
  upSock.on('connect',()=>{
    let h=`CONNECT ${host}:${port} HTTP/1.1\r\nHost: ${host}:${port}\r\n`;
    if(UP_AUTH) h+=`Proxy-Authorization: ${UP_AUTH}\r\n`;
    h+='\r\n'; upSock.write(h);
  });
  let acc=Buffer.alloc(0);
  const onData=(chunk)=>{
    if(done) return;
    acc=Buffer.concat([acc,chunk]);
    const idx=acc.indexOf('\r\n\r\n'); if(idx===-1) return;
    const header=acc.subarray(0,idx).toString('latin1');
    const code=parseInt((header.split(' ')[1]||'0'),10);
    const rest=acc.subarray(idx+4);
    upSock.removeListener('data',onData); done=true;
    if(code!==200){ try{clientSocket.end('HTTP/1.1 '+(code||502)+' X\r\n\r\n');}catch(_){}; try{upSock.destroy();}catch(_){}; return; }
    try{clientSocket.write('HTTP/1.1 200 Connection Established\r\n\r\n');}catch(_){}
    if(head&&head.length){try{upSock.write(head);}catch(_){}}
    if(rest.length){try{clientSocket.write(rest);}catch(_){}}
    upSock.pipe(clientSocket); clientSocket.pipe(upSock);
    clientSocket.on('error',()=>{try{upSock.destroy();}catch(_){}});
    upSock.on('error',()=>{try{clientSocket.destroy();}catch(_){}});
    clientSocket.on('close',()=>{try{upSock.destroy();}catch(_){}});
    upSock.on('close',()=>{try{clientSocket.destroy();}catch(_){}});
  };
  upSock.on('data',onData);
  upSock.on('error',()=>{try{clientSocket.destroy();}catch(_){}});
  clientSocket.on('error',()=>{try{upSock.destroy();}catch(_){}});
});
server.listen(LISTEN,'127.0.0.1',()=>console.log('ready'));
