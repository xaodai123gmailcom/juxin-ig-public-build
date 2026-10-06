import assert from "node:assert/strict";
import test from "node:test";
import {
  centeredChildWindowBounds,
  classifyRendererNavigation,
  isAllowedDevelopmentRendererUrl,
  normalizedExternalHttpsUrl,
  normalizedInstagramProfilePreviewUrl,
  normalizedInstagramPreviewNavigationUrl,
} from "../../dist-electron/navigation-policy.js";

test("development renderer accepts only loopback HTTP", () => {
  assert.equal(isAllowedDevelopmentRendererUrl("http://127.0.0.1:5173/"), true);
  assert.equal(isAllowedDevelopmentRendererUrl("http://localhost:5173/"), false);
  assert.equal(isAllowedDevelopmentRendererUrl("https://127.0.0.1:5173/"), false);
  assert.equal(isAllowedDevelopmentRendererUrl("http://example.com/"), false);
  assert.equal(isAllowedDevelopmentRendererUrl("not-a-url"), false);
});

test("renderer navigation stays local or opens only credential-free HTTPS externally", () => {
  const trustedFile = "file:///C:/Program%20Files/Juxin/renderer/dist/index.html";
  assert.equal(classifyRendererNavigation(`${trustedFile}#/review`, trustedFile), "local");
  assert.equal(classifyRendererNavigation("https://www.instagram.com/example/", trustedFile), "external-https");
  assert.equal(classifyRendererNavigation("https://user:secret@example.com/", trustedFile), "deny");
  assert.equal(classifyRendererNavigation("http://example.com/", trustedFile), "deny");
  assert.equal(classifyRendererNavigation("javascript:alert(1)", trustedFile), "deny");
  assert.equal(classifyRendererNavigation("file:///C:/Windows/System32/calc.exe", trustedFile), "deny");
  assert.equal(classifyRendererNavigation("file://attacker/C:/Program%20Files/Juxin/renderer/dist/index.html#/review", trustedFile), "deny");
  assert.equal(normalizedExternalHttpsUrl("  https://example.com/a b  "), "https://example.com/a%20b");
  assert.equal(normalizedExternalHttpsUrl("https://user:secret@example.com/"), null);
});

test("development navigation cannot escape its exact loopback document", () => {
  const trustedDev = "http://127.0.0.1:5173/";
  assert.equal(classifyRendererNavigation("http://127.0.0.1:5173/#/history", trustedDev), "local");
  assert.equal(classifyRendererNavigation("http://127.0.0.1:5173/admin", trustedDev), "deny");
  assert.equal(classifyRendererNavigation("http://127.0.0.1:5174/", trustedDev), "deny");
});

test("only strict Instagram profile links qualify for the owned app preview", () => {
  assert.equal(
    normalizedInstagramProfilePreviewUrl("https://www.instagram.com/example.user_7/"),
    "https://www.instagram.com/example.user_7/",
  );
  assert.equal(
    normalizedInstagramProfilePreviewUrl("https://instagram.com/Example_User"),
    "https://www.instagram.com/Example_User/",
  );
  for (const rejected of [
    "http://www.instagram.com/example/",
    "https://user:secret@www.instagram.com/example/",
    "https://www.instagram.com:444/example/",
    "https://instagram.com.evil.example/example/",
    "https://evilinstagram.com/example/",
    "https://www.instagram.com/example/followers/",
    "https://www.instagram.com/example/?next=secret",
    "https://www.instagram.com/example/#private",
    "https://www.instagram.com/example%2Ffollowers/",
    "https://www.instagram.com/accounts/",
  ]) assert.equal(normalizedInstagramProfilePreviewUrl(rejected), null, rejected);
});

test("the isolated preview may navigate only inside exact Instagram HTTPS origins", () => {
  assert.equal(
    normalizedInstagramPreviewNavigationUrl("https://instagram.com/accounts/login/?next=%2Fexample%2F"),
    "https://www.instagram.com/accounts/login/?next=%2Fexample%2F",
  );
  assert.equal(
    normalizedInstagramPreviewNavigationUrl("https://www.instagram.com/example/followers/"),
    "https://www.instagram.com/example/followers/",
  );
  assert.equal(normalizedInstagramPreviewNavigationUrl("https://l.instagram.com/?u=https://example.com"), null);
  assert.equal(normalizedInstagramPreviewNavigationUrl("javascript:alert(1)"), null);
  assert.equal(normalizedInstagramPreviewNavigationUrl("https://user@www.instagram.com/example/"), null);
});

test("the app preview is centered on its owner and clamped to the active work area", () => {
  assert.deepEqual(
    centeredChildWindowBounds(
      { x: 100, y: 40, width: 1600, height: 920 },
      { x: 0, y: 0, width: 1920, height: 1040 },
    ),
    { x: 360, y: 110, width: 1080, height: 780 },
  );
  assert.deepEqual(
    centeredChildWindowBounds(
      { x: 1850, y: 940, width: 1180, height: 760 },
      { x: 1920, y: 0, width: 1280, height: 1024 },
    ),
    { x: 1920, y: 344, width: 1080, height: 680 },
  );
});
