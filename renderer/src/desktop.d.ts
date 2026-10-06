import type { CollectorCoreBridge } from "./core-client";

export {};

declare global {
  interface Window {
    collectorCore?: CollectorCoreBridge;
  }
}
