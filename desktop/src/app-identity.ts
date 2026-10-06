import { join } from 'node:path';

export const PRODUCT_NAME = '聚鑫国际';
export const STABLE_APPLICATION_ID = 'com.juxin.igaudiencecollector.newgen';
const LEGACY_PRODUCT_NAME = '聚鑫国际 新一代 IG 采集器';
const STABLE_DATA_NAME = 'juxin-ig-audience-collector-newgen';

export function selectUserDataDirectory(
  appData: string,
  current: string,
  hasUserData: (directory:string)=>boolean,
  hasDatabase?: (directory:string)=>boolean,
): string {
  // Keep the existing database and OS-encrypted login store together.
  // A settings-only directory created under a new product name must not hide
  // the historical database and silently initialize an empty dedupe ledger.
  // If several databases exist, preserve the original directory preference;
  // selecting a directory never merges or moves either database or settings.
  const candidates=[current,join(appData,LEGACY_PRODUCT_NAME),join(appData,STABLE_DATA_NAME)];
  return (hasDatabase ? candidates.find(hasDatabase) : undefined)
    || candidates.find(hasUserData)
    || join(appData,STABLE_DATA_NAME);
}
