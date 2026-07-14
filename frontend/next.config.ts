/* 配置 Turbopack 工作区根目录，并允许本机局域网地址访问开发服务器。 */

import type { NextConfig } from "next";
import path from "path";
import os from "os";

// 收集所有非内部 IPv4 地址，供局域网设备访问开发服务。
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
    root: path.join(__dirname, ".."),
  },
  allowedDevOrigins: [
    "localhost",
    "127.0.0.1",
    ...getLocalIPs(),
  ],
};

export default nextConfig;
