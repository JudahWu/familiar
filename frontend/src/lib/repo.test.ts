import { describe, expect, it } from "vitest";

import { normalizeRepoFullName } from "./repo";

describe("normalizeRepoFullName", () => {
  it("accepts owner/repo", () => {
    expect(normalizeRepoFullName("langchain-ai/langgraph")).toBe("langchain-ai/langgraph");
  });

  it("keeps the host for github URLs", () => {
    expect(normalizeRepoFullName("https://github.com/withmartian/code-review-benchmark")).toBe(
      "github.com/withmartian/code-review-benchmark",
    );
  });

  it("strips the host for gitee URLs (default platform)", () => {
    expect(normalizeRepoFullName("https://gitee.com/msb-goldbin/default_repo.git")).toBe(
      "msb-goldbin/default_repo",
    );
  });

  it("accepts gitlab URLs including nested groups", () => {
    expect(normalizeRepoFullName("https://gitlab.com/group/sub/repo.git")).toBe(
      "gitlab.com/group/sub/repo",
    );
  });

  it("accepts ssh style addresses", () => {
    expect(normalizeRepoFullName("git@github.com:foo/bar.git")).toBe("github.com/foo/bar");
  });

  it("rejects invalid input", () => {
    expect(normalizeRepoFullName("not-a-repo")).toBeNull();
  });
});
