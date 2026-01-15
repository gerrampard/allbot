(function () {
    const currentProxyEl = document.getElementById("current-proxy");
    const lastUpdatedEl = document.getElementById("last-updated");
    const tbody = document.getElementById("nodes-tbody");
    const btnRefresh = document.getElementById("btn-refresh");
    const btnRecheckAll = document.getElementById("btn-recheck-all");
    const btnApply = document.getElementById("btn-apply");
    const btnDisable = document.getElementById("btn-disable");

    if (!currentProxyEl || !tbody || !btnRefresh || !btnRecheckAll || !btnApply || !btnDisable) {
        return;
    }

    let nodes = [];
    let selectedUrl = "";
    let currentProxy = "";
    const checkingUrls = new Set();

    function showToast(type, message) {
        if (typeof window.showToast === "function") {
            window.showToast(type === "error" ? "错误" : "提示", message, type === "error" ? "danger" : "info");
            return;
        }
        alert(message);
    }

    function normalizeUrl(url) {
        url = (url || "").trim();
        if (!url) {
            return "";
        }
        if (!url.endsWith("/")) {
            url += "/";
        }
        return url;
    }

    async function fetchJson(url, options = {}) {
        const resp = await fetch(url, {
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            ...options,
        });
        const data = await resp.json();
        if (!resp.ok || !data.success) {
            throw new Error(data.error || data.message || `请求失败: ${resp.status}`);
        }
        return data;
    }

    function setLoading(text) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="text-center text-muted py-4">
                    <div class="spinner-border text-primary" role="status" aria-hidden="true"></div>
                    <div class="mt-2">${text}</div>
                </td>
            </tr>
        `;
    }

    function render() {
        btnApply.disabled = !selectedUrl;
        const shownCurrent = currentProxy ? currentProxy : "(直连)";
        currentProxyEl.textContent = shownCurrent;

        if (!nodes.length) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-muted py-4">暂无节点数据</td>
                </tr>
            `;
            return;
        }

        tbody.innerHTML = "";
        for (const item of nodes) {
            const proxyUrl = normalizeUrl(item.url);
            const upstreamLatency = typeof item.latency === "number" ? `${item.latency} ms` : "-";
            const location = item.location || "-";
            const tag = item.tag || "-";

            const tr = document.createElement("tr");

            const isSelected = selectedUrl === proxyUrl;
            const isCurrent = normalizeUrl(currentProxy) === proxyUrl;
            const isChecking = checkingUrls.has(proxyUrl) || item.check?.loading;

            const checkBadge = isChecking
                ? `<span class="badge bg-warning text-dark">检测中</span>`
                : (item.check
                    ? (item.check.ok
                        ? `<span class="badge bg-success">OK ${item.check.elapsed_ms}ms</span>`
                        : `<span class="badge bg-danger">FAIL</span>`)
                    : `<span class="badge bg-secondary">未检测</span>`);

            tr.innerHTML = `
                <td>
                    <input class="form-check-input" type="radio" name="proxy-node" ${isSelected ? "checked" : ""} />
                </td>
                <td>
                    <div class="d-flex flex-column">
                        <div class="fw-semibold">${proxyUrl}${isCurrent ? " <span class=\"badge bg-primary ms-1\">当前</span>" : ""}</div>
                    </div>
                </td>
                <td>${upstreamLatency}</td>
                <td>${checkBadge}</td>
                <td>${location}</td>
                <td>${tag}</td>
                <td>
                    <button type="button" class="btn btn-sm btn-outline-primary node-check-btn" ${isChecking ? "disabled" : ""}>
                        ${isChecking ? "检测中" : "检测"}
                    </button>
                </td>
            `;

            tr.querySelector("input[type=radio]").addEventListener("change", () => {
                selectedUrl = proxyUrl;
                render();
            });

            tr.querySelector(".node-check-btn").addEventListener("click", async (event) => {
                event.preventDefault();
                event.stopPropagation();
                await checkOne(proxyUrl);
            });

            tbody.appendChild(tr);
        }
    }

    async function loadCurrent() {
        const data = await fetchJson("/api/github-proxy/current");
        currentProxy = data.data.github_proxy || "";
    }

    async function loadNodes(refresh = false) {
        setLoading("加载节点列表中...");
        const data = await fetchJson(`/api/github-proxy/nodes?refresh=${refresh ? "true" : "false"}`);
        nodes = data.data.nodes || [];
        lastUpdatedEl.textContent = `更新时间：${new Date().toLocaleString()}`;
    }

    async function checkOne(url) {
        const normalizedUrl = normalizeUrl(url);
        const idx = nodes.findIndex((n) => normalizeUrl(n.url) === normalizedUrl);
        if (idx < 0) {
            return null;
        }
        checkingUrls.add(normalizedUrl);
        nodes[idx].check = { loading: true };
        render();
        try {
            const data = await fetchJson("/api/github-proxy/check", {
                method: "POST",
                body: JSON.stringify({ url }),
            });
            const result = {
                ok: Boolean(data.data.ok),
                elapsed_ms: data.data.elapsed_ms,
                status_code: data.data.status_code,
                error: data.data.error,
            };
            nodes[idx].check = result;
            return result;
        } catch (e) {
            const result = { ok: false, elapsed_ms: 0, error: String(e?.message || e) };
            nodes[idx].check = result;
            return result;
        } finally {
            checkingUrls.delete(normalizedUrl);
            render();
        }
    }

    async function checkSample(limit = 6) {
        const sample = nodes.slice(0, limit);
        for (const item of sample) {
            const result = await checkOne(item.url);
            if (!result || !result.ok) {
                const targetUrl = normalizeUrl(item.url);
                nodes = nodes.filter((n) => normalizeUrl(n.url) !== targetUrl);
            }
        }
        if (selectedUrl && !nodes.some((n) => normalizeUrl(n.url) === selectedUrl)) {
            selectedUrl = "";
        }
        render();
    }

    async function apply(url) {
        await fetchJson("/api/github-proxy/apply", {
            method: "POST",
            body: JSON.stringify({ url }),
        });
        await loadCurrent();
        showToast("info", "已更新 github-proxy，重启服务后生效");
    }

    btnRefresh.addEventListener("click", async () => {
        try {
            await loadNodes(true);
            render();
        } catch (e) {
            showToast("error", String(e?.message || e));
        }
    });

    btnRecheckAll.addEventListener("click", async () => {
        try {
            btnRecheckAll.disabled = true;
            await checkSample(6);
        } catch (e) {
            showToast("error", String(e?.message || e));
        } finally {
            btnRecheckAll.disabled = false;
        }
    });

    btnApply.addEventListener("click", async () => {
        if (!selectedUrl) {
            return;
        }
        try {
            btnApply.disabled = true;
            await apply(selectedUrl);
            render();
        } catch (e) {
            showToast("error", String(e?.message || e));
        } finally {
            btnApply.disabled = !selectedUrl;
        }
    });

    btnDisable.addEventListener("click", async () => {
        try {
            await apply("");
            selectedUrl = "";
            render();
        } catch (e) {
            showToast("error", String(e?.message || e));
        }
    });

    (async () => {
        try {
            await loadCurrent();
            await loadNodes(false);
            render();
        } catch (e) {
            showToast("error", String(e?.message || e));
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-muted py-4">加载失败：${String(e?.message || e)}</td>
                </tr>
            `;
        }
    })();
})();
