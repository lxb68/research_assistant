import { API_BASE_URL } from "@/lib/constants";

export function buildApiUrl(path: string): URL {
  return new URL(path, API_BASE_URL);
}
