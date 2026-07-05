/* Models pane: model CRUD, clone, set-default, connection test.
 * Extracted from settings.html — logic unchanged. Uses ModelsCache so the
 * General pane's model selects stay in sync via 'models:changed'. */

window.settingsModels = {
    models: [],
    searchQuery: "",
    _currentTestModelId: null,

    async init() {
        await this.load();
    },

    async load() {
        try {
            this.models = await ModelsCache.get();
            this.render();
        } catch (error) {
            console.error("Failed to load models:", error);
        }
    },

    async reload() {
        ModelsCache.invalidate();
        await this.load();
    },

    render() {
        const modelsList = document.getElementById("models-list");
        const q = this.searchQuery.toLowerCase().trim();
        const filtered = q
            ? this.models.filter(
                  (m) =>
                      (m.name || "").toLowerCase().includes(q) ||
                      (m.provider || "").toLowerCase().includes(q) ||
                      (m.model_name || "").toLowerCase().includes(q) ||
                      (m.type || "").toLowerCase().includes(q),
              )
            : this.models;

        if (filtered.length === 0) {
            modelsList.innerHTML =
                '<p class="text-gray-500 text-center py-4 col-span-full">No models match your search.</p>';
            return;
        }

        modelsList.innerHTML = filtered
            .map((model) => {
                const typeColors =
                    model.type === "remote"
                        ? "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300"
                        : "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300";
                const enabledColors = model.enabled
                    ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300"
                    : "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300";
                const defaultBorder = model.is_default
                    ? "border-indigo-400 dark:border-indigo-500 ring-1 ring-indigo-200 dark:ring-indigo-800"
                    : "border-gray-200 dark:border-gray-700";
                return `
        <div class="model-card bg-white dark:bg-gray-800 rounded-lg border ${defaultBorder} p-3 hover:shadow-sm transition-shadow flex flex-col gap-2">
            <!-- Top row: name + actions (delete lives at the bottom) -->
            <div class="flex items-start justify-between gap-2">
                <h4 class="font-semibold text-sm text-gray-900 dark:text-gray-100 truncate min-w-0">${model.name}</h4>
                <div class="flex flex-row items-center gap-1.5 shrink-0">
                    ${!model.is_default ? `<button class="p-1 rounded border border-amber-400 dark:border-amber-500 text-amber-500 dark:text-amber-400 bg-transparent cursor-pointer hover:bg-amber-50 dark:hover:bg-amber-950/50 transition-colors" onclick="settingsModels.setDefault('${model.id}')" title="Set Default"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"/></svg></button>` : ""}
                    <button class="p-1 rounded border border-gray-300 dark:border-gray-600 bg-transparent cursor-pointer text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors" onclick="settingsModels.testConnection('${model.id}')" title="Test"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg></button>
                    <button class="p-1 rounded border border-indigo-400 dark:border-indigo-500 text-indigo-500 dark:text-indigo-400 bg-transparent cursor-pointer hover:bg-indigo-50 dark:hover:bg-indigo-950/50 transition-colors" onclick="settingsModels.edit('${model.id}')" title="Edit"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg></button>
                    <button class="p-1 rounded border border-emerald-400 dark:border-emerald-500 text-emerald-500 dark:text-emerald-400 bg-transparent cursor-pointer hover:bg-emerald-50 dark:hover:bg-emerald-950/50 transition-colors" onclick="settingsModels.clone('${model.id}')" title="Clone"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" stroke-width="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" stroke-width="2" stroke-linecap="round"/></svg></button>
                </div>
            </div>

            <!-- Badges row -->
            <div class="flex flex-wrap items-center gap-1.5">
                ${model.is_default ? '<span class="inline-block bg-indigo-600 text-white px-1.5 py-0.5 rounded text-[10px] font-semibold leading-none">Default</span>' : ""}
                <span class="inline-block px-1.5 py-0.5 rounded text-[11px] font-medium ${typeColors}">${model.type}</span>
                <span class="inline-block px-1.5 py-0.5 rounded text-[11px] font-medium ${enabledColors}">${model.enabled ? "On" : "Off"}</span>
                ${model.thinking ? '<span class="inline-block px-1.5 py-0.5 rounded text-[11px] font-medium bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300">Thinking</span>' : ""}
            </div>

            <!-- Bottom row: provider info + delete -->
            <div class="flex items-end justify-between gap-2">
                <div class="text-[11px] text-gray-500 dark:text-gray-400 truncate min-w-0">
                    ${model.provider} · ${model.model_name}
                </div>
                <button class="p-1 rounded border border-red-400 dark:border-red-500 text-red-400 dark:text-red-400 bg-transparent cursor-pointer hover:bg-red-50 dark:hover:bg-red-950/50 transition-colors shrink-0" onclick="settingsModels.remove('${model.id}')" title="Delete"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg></button>
            </div>
        </div>
    `;
            })
            .join("");
    },

    filter() {
        this.searchQuery = document.getElementById("model-search-input").value;
        this.render();
    },

    showAddModal() {
        document.getElementById("modal-title").textContent = "Add Model";
        document.getElementById("model-form").reset();
        document.getElementById("model-id").value = "";
        openModal("model-modal");
    },

    edit(modelId) {
        const model = this.models.find((m) => m.id === modelId);
        if (!model) return;

        document.getElementById("modal-title").textContent = "Edit Model";
        document.getElementById("model-id").value = model.id;
        document.getElementById("model-name").value = model.name || "";
        document.getElementById("model-type").value = model.type || "remote";
        document.getElementById("model-provider").value = model.provider || "custom";
        document.getElementById("model-base-url").value = model.base_url || "";
        document.getElementById("model-api-key").value = model.api_key || "";
        document.getElementById("model-name-param").value = model.model_name || "";
        document.getElementById("model-max-tokens").value = model.max_tokens || 32768;
        document.getElementById("model-timeout").value = model.timeout || 60;
        document.getElementById("model-max-concurrent").value =
            model.model_max_concurrent != null ? model.model_max_concurrent : 1;
        document.getElementById("model-temperature").value =
            model.temperature != null ? model.temperature : "";
        document.getElementById("model-thinking").checked = !!model.thinking;
        document.getElementById("model-thinking-budget").value =
            model.thinking_budget || 0;
        document.getElementById("model-enabled").checked = !!model.enabled;
        document.getElementById("model-is-default").checked = !!model.is_default;
        document.getElementById("model-vision-supported").checked =
            !!model.vision_supported;
        document.getElementById("model-api-format").value = model.api_format || "openai";

        this.toggleFields();
        openModal("model-modal");
    },

    toggleFields() {
        const type = document.getElementById("model-type").value;
        const provider = document.getElementById("model-provider").value;
        const apiKeyGroup = document.getElementById("api-key-group");
        if (type === "local" && (provider === "ollama" || provider === "llama.cpp")) {
            apiKeyGroup.style.display = "none";
        } else {
            apiKeyGroup.style.display = "block";
        }
        const idHint = document.getElementById("model-id-hint");
        if (idHint) {
            idHint.style.display = type === "remote" ? "block" : "none";
        }
    },

    async save(event) {
        event.preventDefault();

        const modelId = document.getElementById("model-id").value;
        const modelData = {
            name: document.getElementById("model-name").value,
            type: document.getElementById("model-type").value,
            provider: document.getElementById("model-provider").value,
            base_url: document.getElementById("model-base-url").value,
            api_key: document.getElementById("model-api-key").value,
            model_name: document.getElementById("model-name-param").value,
            max_tokens:
                parseInt(document.getElementById("model-max-tokens").value) || 32768,
            timeout: parseInt(document.getElementById("model-timeout").value) || 60,
            model_max_concurrent:
                parseInt(document.getElementById("model-max-concurrent").value) || 0,
            temperature:
                document.getElementById("model-temperature").value !== ""
                    ? parseFloat(document.getElementById("model-temperature").value)
                    : null,
            thinking: document.getElementById("model-thinking").checked ? 1 : 0,
            thinking_budget:
                parseInt(document.getElementById("model-thinking-budget").value) || 0,
            enabled: document.getElementById("model-enabled").checked ? 1 : 0,
            is_default: document.getElementById("model-is-default").checked ? 1 : 0,
            vision_supported: document.getElementById("model-vision-supported").checked
                ? 1
                : 0,
            api_format: document.getElementById("model-api-format").value,
        };

        try {
            let result;
            if (modelId) {
                result = await apiPut(
                    "/api/models/" + encodeURIComponent(modelId),
                    modelData,
                );
            } else {
                result = await apiPost("/api/models", modelData);
            }

            if (result.success) {
                closeModal("model-modal");
                await this.reload();
                if (window.toast) toast.show("Model saved", "success");
            } else {
                if (window.toast)
                    toast.show("Error: " + (result.error || "Failed to save model"), "error");
            }
        } catch (error) {
            console.error("Failed to save model:", error);
            if (window.toast) toast.show("Failed to save model: " + error.message, "error");
        }
    },

    async setDefault(modelId) {
        try {
            const result = await apiPost(
                "/api/models/" + encodeURIComponent(modelId) + "/set-default",
                {},
            );
            if (result.success) {
                await this.reload();
            } else {
                if (window.toast)
                    toast.show("Error: " + (result.error || "Failed to set default"), "error");
            }
        } catch (error) {
            console.error("Failed to set default:", error);
        }
    },

    async remove(modelId) {
        if (
            !(await showConfirm({
                title: "Delete Model",
                message: "Delete this model? This cannot be undone.",
                confirmText: "Delete",
            }))
        )
            return;

        try {
            const result = await apiDelete("/api/models/" + encodeURIComponent(modelId));
            if (result.success) {
                await this.reload();
            } else {
                if (window.toast)
                    toast.show("Error: " + (result.error || "Failed to delete model"), "error");
            }
        } catch (error) {
            console.error("Failed to delete model:", error);
        }
    },

    async clone(modelId) {
        try {
            const result = await apiPost(
                "/api/models/" + encodeURIComponent(modelId) + "/clone",
                {},
            );
            if (result.success) {
                await this.reload();
                // Open edit modal for the new clone so user can reconfigure
                this.edit(result.model_id);
            } else {
                if (window.toast)
                    toast.show("Error: " + (result.error || "Failed to clone model"), "error");
            }
        } catch (error) {
            console.error("Failed to clone model:", error);
            if (window.toast) toast.show("Failed to clone model: " + error.message, "error");
        }
    },

    /* ---- Connection test ---- */

    // Extract a human-friendly message from the raw API error string.
    // The backend may return: "HTTP 401: {\"error\":\"API key required...\"}"
    _parseTestError(rawError) {
        if (!rawError) return { message: "Unknown error", detail: "" };
        const jsonMatch = rawError.match(
            /\{[^}]*"error"\s*:\s*(?:"([^"]+)"|\{"message"\s*:\s*"([^"]+)"\})/,
        );
        if (jsonMatch) {
            return { message: jsonMatch[1] || jsonMatch[2], detail: rawError };
        }
        const httpMatch = rawError.match(/^HTTP\s+(\d+):\s*(.*)/);
        if (httpMatch) {
            return { message: httpMatch[2] || rawError, detail: rawError };
        }
        return { message: rawError, detail: "" };
    },

    _getTestTroubleshootingTips(statusCode, errorMsg) {
        const msg = (errorMsg || "").toLowerCase();
        const tips = [];
        if (statusCode === 401) {
            tips.push(
                "Your API key is missing or invalid — add a valid API key in the model settings",
            );
            tips.push(
                "Some providers require you to generate an API key from their dashboard first",
            );
        } else if (statusCode === 403) {
            tips.push("Access denied — your API key may not have permission for this endpoint");
            tips.push("Check your provider account for usage limits or billing issues");
        } else if (statusCode === 404) {
            tips.push("The API endpoint was not found — verify the Base URL is correct");
            tips.push("Make sure the model name matches what your provider expects");
        } else if (statusCode === 429) {
            tips.push("Rate limited — your API key has exceeded its request quota");
            tips.push("Wait a moment and try again, or check your provider's rate limits");
        } else if (statusCode && statusCode >= 500) {
            tips.push("The provider's server returned an error — this is usually temporary");
            tips.push(
                "Try again in a few minutes. If it persists, check the provider's status page",
            );
        }
        if (msg.includes("connection") || msg.includes("timeout") || msg.includes("network")) {
            tips.push("Check that the Base URL is reachable from this server");
            tips.push("Verify there is no firewall blocking outbound connections");
        }
        if (
            msg.includes("api key") ||
            msg.includes("unauthorized") ||
            msg.includes("key required")
        ) {
            if (!tips.some((t) => t.toLowerCase().includes("api key"))) {
                tips.push("Add or update your API key in the model's configuration");
            }
        }
        return tips;
    },

    async testConnection(modelId) {
        const testStatus = document.getElementById("connection-test-status");
        const footer = document.getElementById("connection-test-footer");
        const title = document.getElementById("connection-test-title");
        const header = document.getElementById("connection-test-header");

        // Disable the test button for this specific model card
        this._currentTestModelId = modelId;
        const testBtn = document.querySelector(
            `button[onclick*="testConnection('${modelId}')"]`,
        );
        if (testBtn) {
            testBtn.disabled = true;
            testBtn.classList.add("opacity-50", "cursor-not-allowed");
        }

        // Reset header style and show loading state
        if (header) {
            header.className =
                "flex justify-between items-center p-5 border-b border-gray-200 dark:border-gray-600";
        }
        title.textContent = "Testing Connection…";
        title.className = "m-0 text-gray-800 dark:text-gray-100";
        openModal("connection-test-modal");
        testStatus.innerHTML =
            '<div class="text-center py-8">' +
            '<div class="spinner" style="width:32px;height:32px;border-width:3px;"></div>' +
            '<p class="mt-4 text-gray-600 dark:text-gray-400 font-medium">Testing connection…</p>' +
            '<p class="mt-1 text-gray-400 dark:text-gray-500 text-sm">This may take a few seconds</p>' +
            "</div>";
        footer.classList.add("hidden");

        if (window.toast) toast.show("Testing model connection…", "info", 2000);

        try {
            const response = await fetch(
                "/api/models/" + encodeURIComponent(modelId) + "/test",
                {
                    method: "POST",
                },
            );
            const result = await response.json();

            if (result.success) {
                if (window.toast) toast.success("Connected successfully!", 3000);
                if (header) {
                    header.className =
                        "flex justify-between items-center p-5 border-b border-green-200 dark:border-green-700 bg-green-50 dark:bg-green-900/20";
                }
                title.textContent = "Connection Successful";
                title.className = "m-0 text-green-700 dark:text-green-400";
                testStatus.innerHTML =
                    '<div class="p-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-700 rounded-lg">' +
                    '<div class="flex items-center gap-2 mb-3">' +
                    '<svg class="w-6 h-6 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>' +
                    '<span class="text-green-800 dark:text-green-300 font-semibold text-base">Model is reachable</span>' +
                    "</div>" +
                    '<div class="text-green-700 dark:text-green-400 text-sm"><strong>Endpoint:</strong> ' +
                    this._escapeHtml(result.message) +
                    "</div>" +
                    '<div class="text-green-600 dark:text-green-500 text-sm mt-1"><strong>Available models:</strong> ' +
                    result.available_models +
                    "</div>" +
                    (result.available_models > 0
                        ? '<div class="test-tips test-tips-success mt-3"><strong>Tip:</strong> ' +
                          "The provider returned " +
                          result.available_models +
                          " model(s). Your configured model name should match one of them." +
                          "</div>"
                        : "") +
                    "</div>";
            } else {
                const parsed = this._parseTestError(result.error);
                const statusCode = result.status_code;
                const tips = this._getTestTroubleshootingTips(statusCode, parsed.message);

                if (window.toast)
                    toast.error(
                        "Connection failed: " +
                            (statusCode ? "(HTTP " + statusCode + ") " : "") +
                            parsed.message,
                        5000,
                    );

                if (header) {
                    header.className =
                        "flex justify-between items-center p-5 border-b border-red-200 dark:border-red-700 bg-red-50 dark:bg-red-900/20";
                }
                title.textContent = "Connection Failed";
                title.className = "m-0 text-red-700 dark:text-red-400";

                let html =
                    '<div class="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700 rounded-lg">' +
                    '<div class="flex items-start gap-2 mb-2">' +
                    '<svg class="w-6 h-6 text-red-500 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>' +
                    '<div><span class="text-red-800 dark:text-red-300 font-semibold text-base">' +
                    this._escapeHtml(parsed.message) +
                    "</span>" +
                    (statusCode
                        ? '<span class="ml-2 inline-block px-2 py-0.5 text-xs font-bold rounded-full bg-red-200 dark:bg-red-800 text-red-800 dark:text-red-200">HTTP ' +
                          statusCode +
                          "</span>"
                        : "") +
                    "</div></div>";

                if (tips.length > 0) {
                    html += '<div class="test-tips"><strong>Troubleshooting tips:</strong><ul>';
                    tips.forEach((tip) => {
                        html += "<li>" + this._escapeHtml(tip) + "</li>";
                    });
                    html += "</ul></div>";
                }

                if (parsed.detail && parsed.detail !== parsed.message) {
                    html +=
                        '<details class="test-error-detail"><summary class="cursor-pointer text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">Show raw error</summary>' +
                        '<code class="block mt-1 p-2 bg-gray-100 dark:bg-gray-700 rounded text-xs">' +
                        this._escapeHtml(parsed.detail) +
                        "</code></details>";
                }

                html += "</div>";
                testStatus.innerHTML = html;
            }
        } catch (error) {
            if (header) {
                header.className =
                    "flex justify-between items-center p-5 border-b border-red-200 dark:border-red-700 bg-red-50 dark:bg-red-900/20";
            }
            title.textContent = "Connection Failed";
            title.className = "m-0 text-red-700 dark:text-red-400";

            const errorMsg = error.message || "An unexpected network error occurred";
            if (window.toast) toast.error("Connection failed: " + errorMsg, 5000);
            testStatus.innerHTML =
                '<div class="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-700 rounded-lg">' +
                '<div class="flex items-start gap-2 mb-2">' +
                '<svg class="w-6 h-6 text-red-500 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>' +
                '<span class="text-red-800 dark:text-red-300 font-semibold text-base">' +
                this._escapeHtml(errorMsg) +
                "</span>" +
                "</div>" +
                '<div class="test-tips"><strong>Troubleshooting tips:</strong><ul>' +
                "<li>Verify the server can reach the Base URL (check DNS, firewall, or VPN)</li>" +
                "<li>Ensure the Base URL is correct and includes the protocol (http:// or https://)</li>" +
                "<li>Check if the provider's service is currently online</li>" +
                "</ul></div>" +
                "</div>";
        } finally {
            if (this._currentTestModelId === modelId && testBtn) {
                testBtn.disabled = false;
                testBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
        }

        footer.classList.remove("hidden");
    },

    _escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    },

    closeTestModal() {
        closeModal("connection-test-modal");
        if (this._currentTestModelId) {
            const testBtn = document.querySelector(
                `button[onclick*="testConnection('${this._currentTestModelId}')"]`,
            );
            if (testBtn) {
                testBtn.disabled = false;
                testBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
            this._currentTestModelId = null;
        }
    },
};
