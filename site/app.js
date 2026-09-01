const scenarios = {
  repo: {
    kicker: "PHONE REQUEST → VERIFIED PATCH",
    title: "A real Git patch with a real passing unit test.",
    body:
      "The fixture creates a disposable Git-backed service, adds a health check, runs unittest, and records the request, approval, test output, and patch.",
    artifacts: [
      "review/changes.patch",
      "artifact/test-output.txt",
      "receipt.json + receipt.md",
    ],
    receipt: {
      status: "ACCEPTED",
      scope: "deterministic fixture directory",
      capability: "fixture-workspace-write",
      checks: "2 / 2 passed",
      evidence: "patch + test output + phone-to-patch trace",
      retries: "0",
      source:
        "https://github.com/OthmaneBlial/lightclaw/blob/main/showcase/entries/verified-repo-patch/run-card.json",
    },
    command: "lightclaw demo --scenario repo-task --json",
  },
  memory: {
    kicker: "STORE → RESTART → RECALL",
    title: "A fact survives a real SQLite restart.",
    body:
      "The memory fixture stores a synthetic project code in a namespaced local database, closes it, reopens it, and proves lexical recall without a model or network call.",
    artifacts: ["artifact/memory.db", "artifact/recall.json", "private run receipt"],
    receipt: {
      status: "ACCEPTED",
      scope: "deterministic fixture directory",
      capability: "fixture-workspace-write",
      checks: "1 / 1 passed",
      evidence: "recall.json after a real SQLite restart",
      retries: "0",
      source:
        "https://github.com/OthmaneBlial/lightclaw/blob/main/showcase/entries/persistent-memory/run-card.json",
    },
    command: "lightclaw demo --scenario memory --json",
  },
  multi: {
    kicker: "DAG → FAILED CHECK → BOUNDED REPAIR",
    title: "A handoff that records failure instead of hiding it.",
    body:
      "The multi-agent fixture runs dependency-ordered workers, records a failed acceptance check, applies one bounded repair, and keeps the complete audit trail.",
    artifacts: ["worker handoffs", "acceptance evidence", "repair audit + receipt"],
    receipt: {
      status: "ACCEPTED",
      scope: "deterministic fixture directory",
      capability: "fixture-workspace-write",
      checks: "2 / 2 passed",
      evidence: "initial failure + bounded repair + final audit",
      retries: "1 recorded",
      source:
        "https://github.com/OthmaneBlial/lightclaw/blob/main/showcase/entries/audited-multi-agent/run-card.json",
    },
    command: "lightclaw demo --scenario multi-agent --json",
  },
};

const navToggle = document.querySelector(".nav-toggle");
const nav = document.querySelector(".site-nav");

function closeNavigation({ restoreFocus = false } = {}) {
  if (!navToggle || !nav) return;
  navToggle.setAttribute("aria-expanded", "false");
  navToggle.setAttribute("aria-label", "Open navigation");
  nav.classList.remove("open");
  document.body.classList.remove("nav-open");
  if (restoreFocus) navToggle.focus();
}

if (navToggle && nav) {
  navToggle.addEventListener("click", () => {
    const willOpen = navToggle.getAttribute("aria-expanded") !== "true";
    navToggle.setAttribute("aria-expanded", String(willOpen));
    navToggle.setAttribute("aria-label", willOpen ? "Close navigation" : "Open navigation");
    nav.classList.toggle("open", willOpen);
    document.body.classList.toggle("nav-open", willOpen);
    if (willOpen) nav.querySelector("a")?.focus();
  });

  nav.addEventListener("click", (event) => {
    if (event.target.closest("a")) closeNavigation();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && nav.classList.contains("open")) {
      closeNavigation({ restoreFocus: true });
    }
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 760) closeNavigation();
  });
}

const scenarioTabs = [...document.querySelectorAll("[data-scenario]")];
const scenarioKicker = document.querySelector("[data-scenario-kicker]");
const scenarioTitle = document.querySelector("[data-scenario-title]");
const scenarioBody = document.querySelector("[data-scenario-body]");
const scenarioArtifacts = document.querySelector("[data-scenario-artifacts]");
const scenarioCommand = document.querySelector("#scenario-command");
const receiptStatus = document.querySelector("[data-receipt-status]");
const receiptScope = document.querySelector("[data-receipt-scope]");
const receiptCapability = document.querySelector("[data-receipt-capability]");
const receiptChecks = document.querySelector("[data-receipt-checks]");
const receiptEvidence = document.querySelector("[data-receipt-evidence]");
const receiptRetries = document.querySelector("[data-receipt-retries]");
const receiptSource = document.querySelector("[data-receipt-source]");

function selectScenario(name, { focus = false } = {}) {
  const scenario = scenarios[name];
  const selectedTab = scenarioTabs.find((tab) => tab.dataset.scenario === name);
  if (!scenario || !selectedTab) return;

  scenarioTabs.forEach((tab) => {
    const selected = tab === selectedTab;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });

  scenarioKicker.textContent = scenario.kicker;
  scenarioTitle.textContent = scenario.title;
  scenarioBody.textContent = scenario.body;
  scenarioCommand.textContent = scenario.command;
  receiptStatus.textContent = scenario.receipt.status;
  receiptScope.textContent = scenario.receipt.scope;
  receiptCapability.textContent = scenario.receipt.capability;
  receiptChecks.textContent = scenario.receipt.checks;
  receiptEvidence.textContent = scenario.receipt.evidence;
  receiptRetries.textContent = scenario.receipt.retries;
  receiptSource.href = scenario.receipt.source;
  scenarioArtifacts.replaceChildren(
    ...scenario.artifacts.map((artifact) => {
      const item = document.createElement("li");
      item.textContent = artifact;
      return item;
    }),
  );

  if (focus) selectedTab.focus();
}

scenarioTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectScenario(tab.dataset.scenario));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
      return;
    }
    event.preventDefault();
    let nextIndex = index;
    if (["ArrowRight", "ArrowDown"].includes(event.key)) nextIndex = (index + 1) % scenarioTabs.length;
    if (["ArrowLeft", "ArrowUp"].includes(event.key)) nextIndex = (index - 1 + scenarioTabs.length) % scenarioTabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = scenarioTabs.length - 1;
    selectScenario(scenarioTabs[nextIndex].dataset.scenario, { focus: true });
  });
});

selectScenario("repo");

const toast = document.querySelector(".copy-toast");
let toastTimer;

function announceCopy(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 1800);
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.append(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error("Copy command was rejected");
}

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    const label = button.querySelector("span");
    const original = label?.textContent || "Copy";
    try {
      await copyText(target.textContent.trim());
      if (label) label.textContent = "Copied";
      announceCopy("Command copied to clipboard");
    } catch {
      if (label) label.textContent = "Select text";
      announceCopy("Copy unavailable — select the command manually");
    }
    setTimeout(() => {
      if (label) label.textContent = original;
    }, 1800);
  });
});

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const reveals = document.querySelectorAll(".reveal");

if (reduceMotion || !("IntersectionObserver" in window)) {
  reveals.forEach((element) => element.classList.add("is-visible"));
} else {
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: "0px 0px -8%", threshold: 0.08 },
  );
  reveals.forEach((element) => observer.observe(element));
}
