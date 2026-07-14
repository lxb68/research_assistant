/* 基于统一后端地址构造 API URL，避免各组件手工拼接。 */

import { API_BASE_URL } from "@/lib/constants";

/** 基于统一后端地址构造 API URL。 */
export function buildApiUrl(path: string): URL {
  return new URL(path, API_BASE_URL);
}
