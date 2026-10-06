/** Keep the real engine/OS tokens, independent of the localized app name. */
export function accountUserAgent(value: string): string {
  const engine = value.match(/^(Mozilla\/5\.0 \([^)]*\) AppleWebKit\/[\d.]+ \(KHTML, like Gecko\))/);
  const chrome = value.match(/\bChrome\/[\d.]+/);
  const safari = value.match(/\bSafari\/[\d.]+/);
  if (!engine || !chrome || !safari) throw new Error('内置浏览器版本信息无效，请重新启动软件');
  return `${engine[1]} ${chrome[0]} ${safari[0]}`;
}
