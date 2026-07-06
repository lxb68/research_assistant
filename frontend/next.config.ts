import type { NextConfig } from "next";
import path from "path";
import os from 'os'; // 推荐使用 import 而非 require

// 获取所有非内部 IPv4 地址
function getLocalIPs(): string[] {
  const ips: string[] = [];
  const interfaces = os.networkInterfaces();
  for (const ifaceName of Object.keys(interfaces)) {
    const ifaceList = interfaces[ifaceName];
    if (!ifaceList) continue;
    for (const iface of ifaceList) {
      if (iface.family === "IPv4" && !iface.internal) {
        ips.push(iface.address);
      }
    }
  }
  return ips;
}

const nextConfig: NextConfig = {
  turbopack: {
    root: path.join(__dirname, '..'),
  },
  allowedDevOrigins: [
    'localhost',
    '127.0.0.1',
    ...getLocalIPs(),
  ],
};

export default nextConfig; // 只保留这一种导出