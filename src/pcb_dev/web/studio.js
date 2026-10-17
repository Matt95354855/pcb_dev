(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let accessToken = "";
  let latestState = null;
  let requestPending = false;
  let pollingTimer = null;
  let conversationKey = "";
  const statusLabels = {new: "À vous de commencer", awaiting_input: "Une précision est attendue", brief_proposed: "Brief à valider", working: "Conception en cours", blocked: "Un point reste à résoudre", review_ready: "Brouillon à examiner", patch_review: "Corrections à examiner", error: "Action interrompue"};
  const number = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
  const tokens = (value) => `${new Intl.NumberFormat("fr-FR").format(number(value))} token${number(value) > 1 ? "s" : ""}`;
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function showError(message) {
    $("error-banner").textContent = message;
    $("error-banner").hidden = !message;
  }
  async function api(path, data) {
    const headers = {Accept: "application/json"};
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const options = {headers, credentials: "same-origin", cache: "no-store"};
    if (data !== undefined) {
      headers["Content-Type"] = "application/json";
      options.method = "POST";
      options.body = JSON.stringify(data);
    }
    const response = await fetch(path, options);
    if (response.status === 401) {
      accessToken = "";
      if (!$("auth-dialog").open) $("auth-dialog").showModal();
      throw new Error("Saisissez la clé d’accès de cette session.");
    }
    let result;
    try { result = await response.json(); } catch { throw new Error("Le studio a renvoyé une réponse illisible."); }
    if (!response.ok) throw new Error(result.error || "Cette action n’a pas abouti.");
    return result;
  }
  function renderConversation(conversation) {
    const items = Array.isArray(conversation) ? conversation : [];
    const key = JSON.stringify(items);
    if (key === conversationKey) return;
    conversationKey = key;
    if (!items.length) return;
    const target = $("conversation");
    target.replaceChildren();
    for (const message of items) {
      if (!message || typeof message.content !== "string") continue;
      const provider = String(message.provider || "").toLowerCase();
      const user = message.role === "user";
      const claude = provider.includes("claude");
      const article = element("article", `message${user ? " user" : claude ? " claude" : ""}`);
      const label = user ? "Vous" : claude ? "Avis de Claude" : provider === "demo" ? "Assistant · démonstration" : "Qwen · conception";
      article.append(element("div", "message-label", label), element("div", "message-text", message.content));
      target.append(article);
    }
    target.scrollTop = target.scrollHeight;
  }
  function listSection(title, values, className = "") {
    if (!Array.isArray(values) || !values.length) return null;
    const section = element("section", `detail-section ${className}`.trim());
    section.append(element("h3", "", title));
    const list = element("ul");
    for (const value of values) if (typeof value === "string") list.append(element("li", "", value));
    section.append(list);
    return section;
  }
  function renderBrief(state) {
    const brief = state.brief;
    const target = $("brief-content");
    if (!brief || typeof brief !== "object") {
      target.className = "empty-state";
      target.textContent = "Le brief apparaîtra après votre premier échange.";
    } else {
      target.className = "";
      target.replaceChildren();
      if (brief.summary) target.append(element("p", "brief-summary", brief.summary));
      for (const section of [listSection("Ce que la carte doit faire", brief.requirements), listSection("À préciser ensemble", brief.questions, "question-list"), listSection("Hypothèses à confirmer", brief.assumptions), listSection("Pistes proposées", brief.suggestions)]) if (section) target.append(section);
      if (brief.approved) target.append(element("p", "approved-note", "✓ Cette version du brief a été validée."));
    }
    $("approve-button").disabled = requestPending || Boolean(state.studio?.busy) || !brief || !brief.hash || brief.approved === true;
    const plan = $("plan-content");
    plan.replaceChildren();
    if (Array.isArray(state.plan?.steps) && state.plan.steps.length) {
      plan.append(element("h3", "plan-heading", "Les prochaines étapes"));
      for (const [index, step] of state.plan.steps.entries()) {
        if (step && typeof step.description === "string") plan.append(element("div", "plan-step", `${index + 1}. ${step.description}`));
      }
    }
  }
  function renderDesign(state) {
    const target = $("design-content");
    const root = state.design?.root;
    target.replaceChildren();
    if (!root || typeof root !== "object") {
      target.className = "empty-state";
      target.textContent = "Votre circuit sera présenté ici lorsqu’un modèle sera disponible.";
    } else {
      target.className = "";
      let count = 0;
      function block(node, depth = 0) {
        if (!node || typeof node !== "object" || depth > 32 || count++ > 300) return;
        const card = element("article", "block-card");
        card.append(element("h3", "", node.name || (node.id === "board" ? "Carte principale" : node.id) || "Bloc du circuit"));
        if (node.description) card.append(element("p", "", node.description));
        if (Array.isArray(node.components)) for (const component of node.components) {
          const label = [component.reference || component.id, component.value].filter((part) => typeof part === "string").join(" · ");
          card.append(element("div", "component-line", label || "Composant"));
        }
        target.append(card);
        if (Array.isArray(node.children)) for (const child of node.children) block(child, depth + 1);
      }
      block(root);
    }
    const preflight = state.preflight;
    const status = $("preflight-status");
    status.replaceChildren(element("span", "", "Modèle électrique"), element("span", "", !preflight ? "À venir" : preflight.blocking === true ? "Points à résoudre" : preflight.metadata?.completed === true ? "Contrôlé" : "Non terminé"));
    const row = (id, label, value) => $(id).replaceChildren(element("span", "", label), element("span", "", value));
    const coverage = preflight?.metadata?.physical_ports;
    const covered = coverage && coverage.total_ports > 0 ? `${number(coverage.realized_ports)} / ${number(coverage.total_ports)}` : "Non établies";
    row("physical-status", "Liaisons vers les composants", covered);
    const native = state.native_evidence;
    row("native-status", "Vérification native KiCad", !native ? "Non exécutée" : native.stale ? "À refaire" : native.validation_passed === true && native.status === "passed" ? "Vérifiée" : "Points à résoudre");
    for (const [stage, id, label] of [["erc", "erc-status", "Schéma électrique"], ["drc", "drc-status", "Circuit imprimé"], ["model-parity", "parity-status", "Correspondance des fichiers"]]) {
      const report = Array.isArray(native?.reports) ? native.reports.find((item) => item.stage === stage) : null;
      let result = "Non établi";
      if (native?.stale) result = "À refaire";
      else if (report) {
        const metadata = report.metadata || {};
        const executed = stage === "model-parity" ? metadata.native_netlist_export?.executed === true : metadata.native === true && metadata.evidence?.executed === true;
        result = executed && metadata.completed === true && report.blocking === false ? "Vérifié" : report.blocking === true ? "À résoudre" : "Non établi";
      }
      row(id, label, result);
    }
    const simulation = state.simulation_evidence;
    row("simulation-status", "Simulation", !simulation ? "Non exécutée" : simulation.stale ? "À refaire" : simulation.executed === true && simulation.status === "passed" ? "Calculée · matériel non validé" : simulation.status === "unavailable" ? "Moteur indisponible" : "À résoudre");
    const findings = $("findings");
    findings.replaceChildren();
    if (Array.isArray(preflight?.issues)) for (const issue of preflight.issues) {
      if (typeof issue.message === "string") {
        const message = issue.code === "manufacturer.rules_unverified" ? "Les règles de fabrication du fournisseur n’ont pas été vérifiées. Le format de la carte reste une donnée de préparation." : issue.message;
        findings.append(element("div", `finding${["error", "blocker"].includes(issue.severity) ? " error" : ""}`, message));
      }
    }
  }
  async function downloadArtifact(artifact, button) {
    button.disabled = true;
    try {
      const headers = {};
      if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
      const response = await fetch(`/api/artifact?path=${encodeURIComponent(artifact.relative_path)}`, {headers, cache: "no-store", credentials: "same-origin"});
      if (!response.ok) throw new Error("Ce fichier n’est pas disponible. Vérifiez votre session et réessayez.");
      const url = URL.createObjectURL(await response.blob());
      const link = element("a");
      link.href = url;
      link.download = artifact.relative_path.split("/").pop() || "fichier-projet";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { showError(error.message); } finally { button.disabled = false; }
  }
  function renderArtifacts(state) {
    const target = $("files-content");
    const artifacts = Array.isArray(state.artifacts) ? state.artifacts : [];
    target.replaceChildren();
    target.className = artifacts.length ? "" : "empty-state";
    if (!artifacts.length) target.textContent = "Les fichiers générés et leurs rapports apparaîtront ici.";
    for (const artifact of artifacts) {
      if (!artifact || typeof artifact.relative_path !== "string") continue;
      const filename = artifact.relative_path.split("/").pop();
      const labels = {"design.json": "Modèle du circuit", "design.md": "Description du circuit", "index.html": "Aperçu du projet", "bom.csv": "Nomenclature", "bom-jlcpcb.csv": "Nomenclature fournisseur", "preflight.md": "Rapport électrique", "preflight.json": "Données du rapport électrique", "placement-draft.csv": "Placement provisoire"};
      const kind = filename.endsWith(".kicad_sch") ? "Schéma KiCad" : filename.endsWith(".kicad_pcb") ? "Carte KiCad" : filename.endsWith(".kicad_pro") ? "Projet KiCad" : null;
      const button = element("button", "file-button", `↓ ${labels[filename] || kind || filename}`);
      button.title = artifact.relative_path;
      button.type = "button";
      button.addEventListener("click", () => downloadArtifact(artifact, button));
      target.append(button);
    }
  }
  function changeList(changes) {
    const target = element("div", "design-changes");
    const display = (value) => value === null ? "—" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
    for (const change of Array.isArray(changes) ? changes : []) {
      const row = element("div", "design-change");
      row.append(element("div", "change-path", change.path));
      row.append(element("span", "delta-before", display(change.before)), element("span", "", " → "), element("span", "delta-after", display(change.after)));
      target.append(row);
    }
    return target;
  }
  function renderProposals(state) {
    $("review-policy").textContent = state.review_patches === true ? "Les corrections du manager attendent votre décision avant d’être appliquées." : "Le manager applique les corrections autorisées de manière autonome. Les décisions déjà en attente restent nécessaires.";
    const target = $("patch-proposals");
    const proposals = Array.isArray(state.patch_proposals) ? state.patch_proposals : [];
    target.replaceChildren();
    target.className = proposals.length ? "" : "empty-state";
    if (!proposals.length) target.textContent = "Aucune correction proposée.";
    const labels = {pending: "À examiner", accepted: "Accepté", rejected: "Rejeté", stale: "Périmé · nouvelle proposition nécessaire"};
    for (const [index, proposal] of [...proposals].reverse().entries()) {
      const card = element("article", "proposal-card");
      card.append(element("h3", "", `Lot de corrections ${proposals.length - index}`), element("span", "proposal-state", labels[proposal.status] || "État inconnu"));
      card.append(element("p", "", `Motif proposé par le manager : ${proposal.reason || ""}`));
      card.append(changeList(proposal.changes));
      const added = Array.isArray(proposal.issue_delta?.added) ? proposal.issue_delta.added : [];
      const resolved = Array.isArray(proposal.issue_delta?.resolved) ? proposal.issue_delta.resolved : [];
      card.append(element("p", "", `${resolved.length} constat(s) résolu(s) · ${added.length} nouveau(x). Les contrôles concernent le modèle ; les preuves natives seront à refaire après modification.`));
      if (added.length || resolved.length) {
        const details = element("details");
        details.append(element("summary", "", "Voir les constats qui changent"));
        for (const issue of resolved) details.append(element("p", "finding", `Résolu : ${issue.message}`));
        for (const issue of added) details.append(element("p", "finding", `Nouveau : ${issue.message}`));
        card.append(details);
      }
      if (proposal.status === "pending") {
        const label = element("label", "version-label", "Motif si vous rejetez ce lot");
        const reason = element("textarea");
        reason.id = `reject-reason-${index}`;
        reason.rows = 2;
        reason.maxLength = 4000;
        label.htmlFor = reason.id;
        const buttons = element("div", "proposal-actions");
        const accept = element("button", "primary-button", "Accepter ces modifications");
        const reject = element("button", "secondary-button", "Rejeter ce lot");
        accept.type = reject.type = "button";
        accept.disabled = reject.disabled = requestPending || Boolean(state.studio?.busy);
        accept.addEventListener("click", () => action("/api/patch/accept", {proposal_hash: proposal.proposal_hash}));
        reject.addEventListener("click", () => {
          if (!reason.value.trim()) { reason.focus(); return; }
          action("/api/patch/reject", {proposal_hash: proposal.proposal_hash, reason: reason.value.trim()});
        });
        buttons.append(accept, reject);
        card.append(label, reason, buttons);
      } else if (proposal.decision) {
        card.append(element("p", "decision-note", `Décision humaine enregistrée${proposal.decision.reason ? " : " + proposal.decision.reason : "."}`));
      }
      target.append(card);
    }
  }
  function renderHistory(state) {
    const versions = Array.isArray(state.design_versions) ? state.design_versions : [];
    for (const [id, fallback] of [["version-before", versions[0]?.design_hash], ["version-after", versions.at(-1)?.design_hash]]) {
      const select = $(id);
      const selected = select.value;
      select.replaceChildren();
      for (const [index, version] of versions.entries()) {
        const option = element("option", "", `Version ${index + 1} · ${version.design_hash.slice(0, 10)} · étape ${version.revision}`);
        option.value = version.design_hash;
        select.append(option);
      }
      select.value = versions.some((item) => item.design_hash === selected) ? selected : fallback || "";
      select.disabled = !versions.length;
    }
    $("compare-versions").disabled = !versions.length;
  }
  async function compareVersions() {
    const before = $("version-before").value, after = $("version-after").value;
    if (!before || !after) return;
    $("compare-versions").disabled = true;
    const target = $("version-comparison");
    try {
      const result = await api(`/api/versions/compare?before=${encodeURIComponent(before)}&after=${encodeURIComponent(after)}`);
      target.className = "";
      target.replaceChildren(element("p", "comparison-summary", result.changes.length ? `${result.changes.length} modification(s) entre ces versions.` : "Ces versions ne présentent aucune différence de contenu."), changeList(result.changes));
    } catch (error) {
      target.className = "empty-state";
      target.textContent = error.message;
    } finally { $("compare-versions").disabled = false; }
  }
  function renderUsage(state) {
    const usage = state.usage || {};
    $("qwen-tokens").textContent = tokens(usage.qwen_tokens);
    $("claude-tokens").textContent = tokens(usage.claude_tokens);
    $("claude-available").textContent = tokens(usage.claude_available_tokens);
    $("claude-share").textContent = `${new Intl.NumberFormat("fr-FR", {maximumFractionDigits: 1}).format(number(usage.claude_share) * 100)} % Claude`;
    $("usage-unknown").hidden = !number(usage.unknown_qwen_calls);
    const configured = state.claude_configured === true;
    const available = state.claude_available === true;
    const quarantined = usage.claude_quarantined === true;
    const credit = number(usage.claude_available_tokens) > 0;
    $("review-button").disabled = !configured || !available || quarantined || !credit || requestPending || Boolean(state.studio?.busy);
    $("review-availability").textContent = !configured ? "Claude n’est pas configuré pour cette session." : quarantined ? "Les avis Claude sont suspendus car une consommation n’est pas vérifiée. Consultez le registre du projet." : !credit ? "Claude est configuré. Le budget d’avis se débloque après des échanges Qwen dont l’usage est connu." : !available ? "Claude est temporairement indisponible pour cette session." : "Cet avis utilise le budget Claude affiché. Vous seul pouvez le demander.";
  }
  function render(state) {
    latestState = state;
    const busy = requestPending || Boolean(state.studio?.busy);
    $("project-title").textContent = state.brief?.title || "Une idée de circuit ?";
    $("project-status").textContent = busy ? "Réponse en préparation" : state.studio?.error ? "Action interrompue" : statusLabels[state.status] || "Projet en cours";
    $("demo-banner").hidden = state.demo !== true;
    $("provider-caption").textContent = state.demo === true ? "Explorez le parcours avec un assistant de démonstration." : "Qwen vous accompagne dans le projet.";
    $("working").hidden = !busy;
    $("send-button").disabled = busy;
    $("message").disabled = busy;
    showError(state.studio?.error || state.error || "");
    renderConversation(state.conversation);
    renderBrief(state);
    renderDesign(state);
    renderArtifacts(state);
    renderProposals(state);
    renderHistory(state);
    renderUsage(state);
    clearTimeout(pollingTimer);
    if (state.studio?.busy) pollingTimer = setTimeout(refresh, 800);
  }
  async function refresh() {
    try { render(await api("/api/state")); } catch (error) { showError(error.message); }
  }
  async function action(path, data) {
    if (requestPending || latestState?.studio?.busy) return;
    requestPending = true;
    if (latestState) render(latestState);
    showError("");
    try {
      const result = await api(path, data);
      requestPending = false;
      render(result);
      if (path === "/api/message") $("message").value = "";
    } catch (error) {
      requestPending = false;
      if (latestState) render(latestState);
      showError(error.message);
    }
  }
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("message").value.trim();
    if (text) action("/api/message", {text});
  });
  $("message").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  for (const button of document.querySelectorAll("[data-prompt]")) button.addEventListener("click", () => {
    $("message").value = button.dataset.prompt;
    $("message").focus();
  });
  $("approve-button").addEventListener("click", () => action("/api/approve", {brief_hash: latestState?.brief?.hash}));
  $("review-button").addEventListener("click", () => {
    const question = $("review-question").value.trim();
    if (!question) { $("review-question").focus(); return; }
    action("/api/claude-review", {question});
  });
  for (const name of ["brief", "design", "files", "reviews", "history"]) $("tab-" + name).addEventListener("click", () => {
    for (const other of ["brief", "design", "files", "reviews", "history"]) {
      $("tab-" + other).setAttribute("aria-selected", String(name === other));
      $(other + "-panel").hidden = name !== other;
    }
  });
  $("compare-versions").addEventListener("click", compareVersions);
  $("auth-dialog").addEventListener("cancel", (event) => event.preventDefault());
  $("auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    accessToken = $("access-token").value;
    $("access-token").value = "";
    try {
      const state = await api("/api/state");
      $("auth-dialog").close();
      $("auth-error").hidden = true;
      render(state);
    } catch {
      $("auth-error").hidden = false;
    }
  });
  refresh();
})();
