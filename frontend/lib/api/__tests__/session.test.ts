/**
 * SESSION 28: where the access token lives.
 *
 * It was read from `localStorage` from SESSION 9 onward and **nothing ever
 * wrote it** — there was no sign-in screen, so the browser could reach the
 * platform and authenticate to none of it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  accessToken,
  clearSession,
  isSignedIn,
  refreshToken,
  storeSession,
} from "../session";

function fakeStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

beforeEach(() => {
  vi.stubGlobal("window", { localStorage: fakeStorage() });
});

describe("a session", () => {
  it("starts signed out", () => {
    expect(isSignedIn()).toBe(false);
    expect(accessToken()).toBeNull();
  });

  it("is signed in once a token is stored", () => {
    storeSession("access-1", "refresh-1");
    expect(isSignedIn()).toBe(true);
    expect(accessToken()).toBe("access-1");
    expect(refreshToken()).toBe("refresh-1");
  });

  it("survives without a refresh token", () => {
    // The API may not return one; that is not a reason to fail the sign-in.
    storeSession("access-1", null);
    expect(isSignedIn()).toBe(true);
    expect(refreshToken()).toBeNull();
  });

  it("is signed out again after clearing", () => {
    storeSession("access-1", "refresh-1");
    clearSession();
    expect(isSignedIn()).toBe(false);
    expect(refreshToken()).toBeNull();
  });
});

describe("a browser that refuses to store", () => {
  // A private window, or site data blocked. Every accessor throws.
  beforeEach(() => {
    vi.stubGlobal("window", {
      localStorage: {
        getItem: () => {
          throw new Error("denied");
        },
        setItem: () => {
          throw new Error("denied");
        },
        removeItem: () => {
          throw new Error("denied");
        },
      },
    });
  });

  it("reads as signed out rather than crashing the page", () => {
    expect(() => isSignedIn()).not.toThrow();
    expect(isSignedIn()).toBe(false);
  });

  it("does not fail the sign-in itself", () => {
    // The session simply does not survive a reload, which is better than
    // refusing to sign in at all.
    expect(() => storeSession("a", "b")).not.toThrow();
  });

  it("does not fail signing out", () => {
    expect(() => clearSession()).not.toThrow();
  });
});

describe("server-side rendering", () => {
  it("has no session and does not touch window", () => {
    vi.stubGlobal("window", undefined);
    expect(isSignedIn()).toBe(false);
    expect(accessToken()).toBeNull();
    expect(() => storeSession("a", "b")).not.toThrow();
  });
});

describe("the redirect target after signing in", () => {
  // The sign-in page reads `?next=` so somebody who followed a link to a
  // timetable lands on that timetable. An unchecked value would make this an
  // open redirect: /login?next=https://elsewhere would hand a freshly
  // authenticated browser to another origin.
  function safeNext(raw: string | null): string {
    return raw && raw.startsWith("/") && !raw.startsWith("//")
      ? raw
      : "/dashboard";
  }

  it("keeps a path on this site", () => {
    expect(safeNext("/timetable?period=abc")).toBe("/timetable?period=abc");
  });

  it("refuses an absolute URL", () => {
    expect(safeNext("https://elsewhere.example/steal")).toBe("/dashboard");
  });

  it("refuses a protocol-relative URL", () => {
    // `//elsewhere.example` is another origin wearing a path's clothes.
    expect(safeNext("//elsewhere.example/steal")).toBe("/dashboard");
  });

  it("falls back when there is nothing", () => {
    expect(safeNext(null)).toBe("/dashboard");
    expect(safeNext("")).toBe("/dashboard");
  });
});
