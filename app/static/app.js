(function () {
    let activeRunSource = null;
    let activeRunId = "";
    const scrollStorageKey = "ai-news:scroll-positions";
    const sourceEditorStorageKey = "ai-news:source-editor";

    if ("scrollRestoration" in window.history) {
        window.history.scrollRestoration = "manual";
    }

    function storageRead(key, fallback) {
        try {
            const raw = window.sessionStorage.getItem(key);
            return raw ? JSON.parse(raw) : fallback;
        } catch (_error) {
            return fallback;
        }
    }

    function storageWrite(key, value) {
        try {
            window.sessionStorage.setItem(key, JSON.stringify(value));
        } catch (_error) {
            // Storage is optional; the current interaction remains functional.
        }
    }

    function currentScrollPath() {
        return `${window.location.pathname}${window.location.search}`;
    }

    function saveScrollPosition() {
        const positions = storageRead(scrollStorageKey, {});
        positions[currentScrollPath()] = {
            x: window.scrollX,
            y: window.scrollY,
            updatedAt: Date.now(),
        };
        const recent = Object.entries(positions)
            .sort((a, b) => (b[1].updatedAt || 0) - (a[1].updatedAt || 0))
            .slice(0, 20);
        storageWrite(scrollStorageKey, Object.fromEntries(recent));
    }

    function savedScrollPosition() {
        const positions = storageRead(scrollStorageKey, {});
        return positions[currentScrollPath()] || null;
    }

    function keepControlInPlace(control, mutate) {
        if (!control || typeof mutate !== "function") {
            return;
        }
        const topBefore = control.getBoundingClientRect().top;
        mutate();
        const topAfter = control.getBoundingClientRect().top;
        const delta = topAfter - topBefore;
        if (Math.abs(delta) > 0.5) {
            window.scrollBy({top: delta, left: 0, behavior: "auto"});
        }
        saveScrollPosition();
    }

    function syncCustomModelRequirement(modelSelect) {
        const customModelInput = modelSelect && modelSelect.form
            ? modelSelect.form.querySelector("input[name='custom_model']")
            : null;
        if (!customModelInput) {
            return;
        }
        const manualModelRequired = modelSelect.value === "";
        customModelInput.required = manualModelRequired;
        customModelInput.setAttribute("aria-required", String(manualModelRequired));
    }

    function syncModelSelect(providerSelect) {
        const targetId = providerSelect.dataset.modelTarget;
        const modelSelect = document.getElementById(targetId);
        if (!modelSelect) {
            return;
        }
        const providerModels = window.PROVIDER_MODELS || {};
        const models = providerModels[providerSelect.value] || [];
        const preferred = modelSelect.dataset.defaultModel || modelSelect.value || models[0] || "";
        modelSelect.innerHTML = "";
        const manualOption = document.createElement("option");
        manualOption.value = "";
        manualOption.textContent = "不选择";
        modelSelect.appendChild(manualOption);
        models.forEach((model) => {
            const option = document.createElement("option");
            option.value = model;
            option.textContent = model;
            if (model === preferred) {
                option.selected = true;
            }
            modelSelect.appendChild(option);
        });
        if (modelSelect.options.length && !modelSelect.value) {
            modelSelect.options[0].selected = true;
        }
        syncCustomModelRequirement(modelSelect);
    }

    function setupModelSelectors() {
        document.querySelectorAll("select[data-model-target]").forEach((providerSelect) => {
            if (providerSelect.dataset.bound === "1") {
                syncModelSelect(providerSelect);
                return;
            }
            providerSelect.dataset.bound = "1";
            syncModelSelect(providerSelect);
            providerSelect.addEventListener("change", () => {
                const modelSelect = document.getElementById(providerSelect.dataset.modelTarget);
                if (modelSelect) {
                    modelSelect.dataset.defaultModel = "";
                }
                syncModelSelect(providerSelect);
            });
            const modelSelect = document.getElementById(providerSelect.dataset.modelTarget);
            if (modelSelect && modelSelect.dataset.manualModelBound !== "1") {
                modelSelect.dataset.manualModelBound = "1";
                modelSelect.addEventListener("change", () => {
                    syncCustomModelRequirement(modelSelect);
                });
            }
        });
    }

    function appendLog(logBox, message) {
        const line = message.endsWith("\n") ? message : `${message}\n`;
        logBox.textContent += line;
        const lines = logBox.textContent.split("\n");
        if (lines.length > 500) {
            logBox.textContent = lines.slice(lines.length - 500).join("\n");
        }
        logBox.scrollTop = logBox.scrollHeight;
    }

    const noticeStorageKey = "ai-news:system-notices";

    function readQueuedNotices() {
        try {
            const rawValue = window.sessionStorage.getItem(noticeStorageKey);
            const parsed = rawValue ? JSON.parse(rawValue) : [];
            return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
        } catch (_error) {
            return [];
        }
    }

    function writeQueuedNotices(notices) {
        try {
            window.sessionStorage.setItem(noticeStorageKey, JSON.stringify(notices.slice(-4)));
        } catch (_error) {
            // Ignore storage errors; the live log still contains the system prompt.
        }
    }

    function queueSystemNotice(message) {
        if (!message) {
            return;
        }
        const notices = readQueuedNotices();
        if (!notices.includes(message)) {
            notices.push(message);
            writeQueuedNotices(notices);
        }
    }

    function systemNoticeFromLog(message) {
        if (!message) {
            return "";
        }
        if (message.includes("系统提示：信息抓取任务完成")) {
            return "系统提示：信息抓取任务完成";
        }
        if (message.includes("系统提示：新闻分析完成")) {
            return "系统提示：新闻分析完成";
        }
        return "";
    }

    function systemNoticeFromStatus(status) {
        if (status === "crawled") {
            return "系统提示：信息抓取任务完成";
        }
        if (status === "completed") {
            return "系统提示：新闻分析完成";
        }
        return "";
    }

    function showSystemNotice(message) {
        if (!message) {
            return;
        }
        let stack = document.querySelector("[data-system-notice-stack]");
        if (!stack) {
            stack = document.createElement("div");
            stack.className = "system-notice-stack";
            stack.dataset.systemNoticeStack = "1";
            document.body.appendChild(stack);
        }
        const notice = document.createElement("div");
        notice.className = "system-notice";
        notice.textContent = message;
        stack.appendChild(notice);
        window.setTimeout(() => {
            notice.classList.add("visible");
        }, 20);
        window.setTimeout(() => {
            notice.classList.remove("visible");
            window.setTimeout(() => {
                notice.remove();
                if (!stack.children.length) {
                    stack.remove();
                }
            }, 240);
        }, 5200);
    }

    function showQueuedSystemNotices() {
        const notices = readQueuedNotices();
        if (!notices.length) {
            return;
        }
        try {
            window.sessionStorage.removeItem(noticeStorageKey);
        } catch (_error) {
            writeQueuedNotices([]);
        }
        notices.forEach((message, index) => {
            window.setTimeout(() => showSystemNotice(message), index * 320);
        });
    }

    function formatLocalDateTime(date) {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        const hour = String(date.getHours()).padStart(2, "0");
        const minute = String(date.getMinutes()).padStart(2, "0");
        return `${year}-${month}-${day} ${hour}:${minute}`;
    }

    function setupLocalDateTimes() {
        document.querySelectorAll("[data-local-datetime]").forEach((element) => {
            const value = element.getAttribute("datetime") || "";
            if (!value) {
                return;
            }
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) {
                return;
            }
            element.textContent = formatLocalDateTime(date);
            const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
            if (timeZone) {
                element.title = `本机时区：${timeZone}`;
            }
        });
    }

    function setupRunEvents() {
        const liveRun = document.getElementById("live-run");
        if (!liveRun || liveRun.dataset.live !== "1") {
            return;
        }
        const runId = liveRun.dataset.runId;
        const logBox = document.getElementById("log-box");
        const statusEl = document.getElementById("status-value");
        const summaryEl = document.getElementById("live-status-summary");
        const progressEl = liveRun.querySelector("[data-live-progress]");
        if (!runId || !logBox) {
            return;
        }
        if (activeRunSource && activeRunId === runId) {
            return;
        }
        if (activeRunSource) {
            activeRunSource.close();
        }

        activeRunId = runId;
        activeRunSource = new EventSource(`/runs/${runId}/events`);
        let reloaded = false;

        if (summaryEl) {
            summaryEl.textContent = "实时连接已建立，正在等待任务进度。";
        }
        if (progressEl) {
            progressEl.style.width = "4%";
        }

        activeRunSource.onmessage = (event) => {
            let payload;
            try {
                payload = JSON.parse(event.data);
            } catch (_error) {
                return;
            }
            if (payload.type === "log" && payload.message) {
                appendLog(logBox, payload.message);
                queueSystemNotice(systemNoticeFromLog(payload.message));
            }
            if (payload.type === "status" && statusEl) {
                statusEl.textContent = payload.label || payload.status;
                statusEl.className = `badge status-${payload.status}`;
                if (summaryEl) {
                    const phase = payload.phase ? ` · 阶段：${payload.phase}` : "";
                    const raw = Number.isFinite(payload.raw_count) ? ` · 原始条目：${payload.raw_count}` : "";
                    const structured = Number.isFinite(payload.structured_count) ? ` · 结构化：${payload.structured_count}` : "";
                    summaryEl.textContent = `状态：${payload.label || payload.status}${phase}${raw}${structured}`;
                }
                if (progressEl) {
                    let progress = 6;
                    const crawlMatch = String(payload.phase || "").match(/crawl\s+(\d+)\/(\d+)/);
                    if (crawlMatch) {
                        const done = Number.parseInt(crawlMatch[1], 10);
                        const total = Number.parseInt(crawlMatch[2], 10);
                        progress = total > 0 ? 6 + ((done / total) * 72) : 6;
                    } else if (payload.status === "analyzing") {
                        progress = 88;
                    } else if (payload.status === "crawled") {
                        progress = 100;
                    } else if (payload.status === "completed" || payload.status === "failed") {
                        progress = 100;
                    }
                    progressEl.style.width = `${Math.max(4, Math.min(progress, 100))}%`;
                }
            }
            if (payload.done && !reloaded) {
                queueSystemNotice(systemNoticeFromStatus(payload.status));
                reloaded = true;
                activeRunSource.close();
                activeRunSource = null;
                activeRunId = "";
                saveScrollPosition();
                window.setTimeout(() => window.location.reload(), 900);
            }
        };

        activeRunSource.onerror = () => {
            appendLog(logBox, "实时连接中断，页面会继续保留当前日志。");
            if (summaryEl) {
                summaryEl.textContent = "实时连接中断，请刷新页面查看最新状态。";
            }
            activeRunSource.close();
            activeRunSource = null;
            activeRunId = "";
        };
    }

    function normalizeFilterValue(value) {
        return (value || "").trim().toLowerCase();
    }

    function readCardFilterValues(card, key) {
        const serializedValues = card.dataset[`${key}Values`];
        if (serializedValues) {
            try {
                const parsed = JSON.parse(serializedValues);
                if (Array.isArray(parsed)) {
                    return parsed
                        .map(normalizeFilterValue)
                        .filter(Boolean);
                }
            } catch (_error) {
                // Fall back to the single-value data attribute.
            }
        }
        const value = normalizeFilterValue(card.dataset[key]);
        return value ? [value] : [];
    }

    function readFilterValues(filter) {
        if (filter.dataset.newsFilterMode === "checkboxes") {
            return Array.from(filter.querySelectorAll("input[type='checkbox']:checked"))
                .map((input) => normalizeFilterValue(input.value))
                .filter(Boolean);
        }
        if (filter.matches("select[multiple]")) {
            return Array.from(filter.selectedOptions)
                .map((option) => normalizeFilterValue(option.value))
                .filter(Boolean);
        }
        const value = normalizeFilterValue(filter.value);
        return value ? [value] : [];
    }

    function clearFilter(filter) {
        if (filter.dataset.newsFilterMode === "checkboxes") {
            filter.querySelectorAll("input[type='checkbox']").forEach((input) => {
                input.checked = false;
            });
            filter.dispatchEvent(new Event("change", {bubbles: true}));
            return;
        }
        filter.value = "";
        filter.dispatchEvent(new Event("change", {bubbles: true}));
    }

    function dateFilterStorageKey(picker) {
        const scope = picker.closest("[data-news-filter-scope]");
        const persistKey = scope ? scope.dataset.newsFilterPersistKey : "";
        return persistKey ? `ai-news:date-filter:${persistKey}` : "";
    }

    function readStoredDateFilter(storageKey, availableSet) {
        if (!storageKey) {
            return null;
        }
        try {
            const rawValue = window.sessionStorage.getItem(storageKey);
            if (rawValue === null) {
                return null;
            }
            const parsed = JSON.parse(rawValue);
            if (!Array.isArray(parsed)) {
                return null;
            }
            const validDates = parsed.filter((value) => availableSet.has(value));
            if (parsed.length > 0 && validDates.length === 0) {
                window.sessionStorage.removeItem(storageKey);
                return null;
            }
            return validDates;
        } catch (_error) {
            return null;
        }
    }

    function writeStoredDateFilter(storageKey, dates) {
        if (!storageKey) {
            return;
        }
        try {
            window.sessionStorage.setItem(storageKey, JSON.stringify(dates));
        } catch (_error) {
            // Ignore storage errors; filtering still works for the current page.
        }
    }

    function parseDateValue(value) {
        const parts = (value || "").split("-").map((part) => Number.parseInt(part, 10));
        if (parts.length !== 3 || parts.some((part) => Number.isNaN(part))) {
            return null;
        }
        return new Date(parts[0], parts[1] - 1, parts[2]);
    }

    function formatDateValue(date) {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        return `${year}-${month}-${day}`;
    }

    function monthKey(date) {
        return date.getFullYear() * 12 + date.getMonth();
    }

    function setupDatePickers() {
        document.querySelectorAll("[data-date-picker]").forEach((picker) => {
            if (picker.dataset.bound === "1") {
                return;
            }
            picker.dataset.bound = "1";

            const toggle = picker.querySelector("[data-date-picker-toggle]");
            const panel = picker.querySelector("[data-date-picker-panel]");
            const label = picker.querySelector("[data-date-picker-label]");
            const monthLabel = picker.querySelector("[data-date-picker-month]");
            const grid = picker.querySelector("[data-date-picker-grid]");
            const prevBtn = picker.querySelector("[data-date-picker-prev]");
            const nextBtn = picker.querySelector("[data-date-picker-next]");
            const clearBtn = picker.querySelector("[data-date-picker-clear]");
            const doneBtn = picker.querySelector("[data-date-picker-done]");
            const rangeButtons = Array.from(
                picker.querySelectorAll("[data-date-picker-range]"),
            );
            const values = picker.querySelector("[data-news-filter='date']");
            const checkboxes = values ? Array.from(values.querySelectorAll("input[type='checkbox']")) : [];
            const availableValues = checkboxes.map((input) => input.value).filter(Boolean);
            const availableSet = new Set(availableValues);
            const availableDates = availableValues
                .map(parseDateValue)
                .filter(Boolean)
                .sort((a, b) => a - b);

            if (!toggle || !panel || !label || !monthLabel || !grid || !values || !availableDates.length) {
                return;
            }

            const storageKey = dateFilterStorageKey(picker);
            const storedDates = readStoredDateFilter(storageKey, availableSet);
            if (storedDates !== null) {
                const storedSet = new Set(storedDates);
                checkboxes.forEach((input) => {
                    input.checked = storedSet.has(input.value);
                });
            }

            const checkedDate = checkboxes.find((input) => input.checked);
            let currentMonth = parseDateValue(checkedDate ? checkedDate.value : availableValues[0]) || availableDates[availableDates.length - 1];
            currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth(), 1);
            const minMonth = monthKey(availableDates[0]);
            const maxMonth = monthKey(availableDates[availableDates.length - 1]);

            const selectedValues = () => checkboxes
                .filter((input) => input.checked)
                .map((input) => input.value)
                .sort()
                .reverse();

            const rangeStartDate = (latestDate, range) => {
                const result = new Date(
                    latestDate.getFullYear(),
                    latestDate.getMonth(),
                    latestDate.getDate(),
                );
                if (range === "7d") {
                    result.setDate(result.getDate() - 6);
                    return result;
                }
                const months = {
                    "1m": 1,
                    "3m": 3,
                    "1y": 12,
                }[range];
                if (!months) {
                    return result;
                }
                const day = result.getDate();
                result.setDate(1);
                result.setMonth(result.getMonth() - months);
                const lastDay = new Date(
                    result.getFullYear(),
                    result.getMonth() + 1,
                    0,
                ).getDate();
                result.setDate(Math.min(day, lastDay));
                return result;
            };

            const updateLabel = () => {
                const selected = selectedValues();
                if (!selected.length) {
                    label.textContent = "全部日期";
                } else if (selected.length === 1) {
                    label.textContent = selected[0];
                } else if (selected.length === 2) {
                    label.textContent = selected.join("、");
                } else {
                    label.textContent = `${selected[0]} 等 ${selected.length} 个日期`;
                }
            };

            const render = () => {
                const year = currentMonth.getFullYear();
                const month = currentMonth.getMonth();
                const currentKey = monthKey(currentMonth);
                monthLabel.textContent = `${year}年${month + 1}月`;
                if (prevBtn) {
                    prevBtn.disabled = currentKey <= minMonth;
                }
                if (nextBtn) {
                    nextBtn.disabled = currentKey >= maxMonth;
                }

                grid.innerHTML = "";
                const selected = new Set(selectedValues());
                const firstDay = new Date(year, month, 1);
                const start = new Date(year, month, 1 - firstDay.getDay());
                for (let index = 0; index < 42; index += 1) {
                    const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index);
                    const value = formatDateValue(date);
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "date-picker-day";
                    button.textContent = String(date.getDate());
                    button.dataset.dateValue = value;
                    if (date.getMonth() !== month) {
                        button.classList.add("outside");
                    }
                    if (!availableSet.has(value)) {
                        button.classList.add("no-data");
                        button.disabled = true;
                    }
                    if (selected.has(value)) {
                        button.classList.add("selected");
                        button.setAttribute("aria-pressed", "true");
                    } else {
                        button.setAttribute("aria-pressed", "false");
                    }
                    button.addEventListener("click", () => {
                        const input = checkboxes.find((item) => item.value === value);
                        if (!input) {
                            return;
                        }
                        input.checked = !input.checked;
                        input.dispatchEvent(new Event("change", {bubbles: true}));
                    });
                    grid.appendChild(button);
                }
                updateLabel();
            };

            const closePanel = () => {
                panel.hidden = true;
                toggle.setAttribute("aria-expanded", "false");
            };

            const openPanel = () => {
                document.querySelectorAll("[data-date-picker-panel]").forEach((otherPanel) => {
                    if (otherPanel === panel) {
                        return;
                    }
                    otherPanel.hidden = true;
                    const otherPicker = otherPanel.closest("[data-date-picker]");
                    const otherToggle = otherPicker ? otherPicker.querySelector("[data-date-picker-toggle]") : null;
                    if (otherToggle) {
                        otherToggle.setAttribute("aria-expanded", "false");
                    }
                });
                document.querySelectorAll("[data-multi-select-panel]").forEach((otherPanel) => {
                    otherPanel.hidden = true;
                    const otherSelect = otherPanel.closest("[data-multi-select]");
                    const otherToggle = otherSelect
                        ? otherSelect.querySelector("[data-multi-select-toggle]")
                        : null;
                    if (otherToggle) {
                        otherToggle.setAttribute("aria-expanded", "false");
                    }
                });
                panel.hidden = false;
                toggle.setAttribute("aria-expanded", "true");
                render();
            };

            toggle.addEventListener("click", () => {
                if (panel.hidden) {
                    openPanel();
                } else {
                    closePanel();
                }
            });
            panel.addEventListener("click", (event) => {
                event.stopPropagation();
            });
            if (prevBtn) {
                prevBtn.addEventListener("click", () => {
                    currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 1, 1);
                    render();
                });
            }
            if (nextBtn) {
                nextBtn.addEventListener("click", () => {
                    currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 1);
                    render();
                });
            }
            if (clearBtn) {
                clearBtn.addEventListener("click", () => {
                    checkboxes.forEach((input) => {
                        input.checked = false;
                    });
                    values.dispatchEvent(new Event("change", {bubbles: true}));
                });
            }
            rangeButtons.forEach((button) => {
                button.addEventListener("click", () => {
                    const latestDate = availableDates[availableDates.length - 1];
                    const startDate = rangeStartDate(
                        latestDate,
                        button.dataset.datePickerRange || "",
                    );
                    checkboxes.forEach((input) => {
                        const date = parseDateValue(input.value);
                        input.checked = Boolean(
                            date && date >= startDate && date <= latestDate,
                        );
                    });
                    currentMonth = new Date(
                        latestDate.getFullYear(),
                        latestDate.getMonth(),
                        1,
                    );
                    values.dispatchEvent(new Event("change", {bubbles: true}));
                });
            });
            if (doneBtn) {
                doneBtn.addEventListener("click", closePanel);
            }
            values.addEventListener("change", () => {
                const firstSelected = selectedValues()[0];
                const selectedDate = parseDateValue(firstSelected);
                writeStoredDateFilter(storageKey, selectedValues());
                if (panel.hidden && selectedDate) {
                    currentMonth = new Date(selectedDate.getFullYear(), selectedDate.getMonth(), 1);
                }
                render();
            });
            document.addEventListener("click", (event) => {
                if (!picker.contains(event.target)) {
                    closePanel();
                }
            });
            document.addEventListener("keydown", (event) => {
                if (event.key === "Escape") {
                    closePanel();
                }
            });
            render();
        });
    }

    function setupMultiSelectFilters() {
        document.querySelectorAll("[data-multi-select]").forEach((multiSelect) => {
            if (multiSelect.dataset.bound === "1") {
                return;
            }
            multiSelect.dataset.bound = "1";

            const toggle = multiSelect.querySelector("[data-multi-select-toggle]");
            const panel = multiSelect.querySelector("[data-multi-select-panel]");
            const label = multiSelect.querySelector("[data-multi-select-label]");
            const search = multiSelect.querySelector("[data-multi-select-search]");
            const filter = multiSelect.querySelector("[data-news-filter]");
            const clearBtn = multiSelect.querySelector("[data-multi-select-clear]");
            const doneBtn = multiSelect.querySelector("[data-multi-select-done]");
            const emptyEl = multiSelect.querySelector("[data-multi-select-empty]");
            const optionRows = Array.from(
                multiSelect.querySelectorAll("[data-multi-select-option]"),
            );
            const checkboxes = filter
                ? Array.from(filter.querySelectorAll("input[type='checkbox']"))
                : [];

            if (!toggle || !panel || !label || !search || !filter) {
                return;
            }

            const allLabel = multiSelect.dataset.multiSelectAllLabel || "全部";
            const fieldLabel = multiSelect.closest(".filter-field")
                ?.querySelector(":scope > label")
                ?.textContent.trim() || "筛选";

            const selectedInputs = () => checkboxes.filter((input) => input.checked);

            const updateLabel = () => {
                const selected = selectedInputs();
                const selectedLabels = selected.map((input) => input.value);
                let displayLabel = allLabel;
                if (selectedLabels.length === 1) {
                    [displayLabel] = selectedLabels;
                } else if (selectedLabels.length > 1) {
                    displayLabel = `${selectedLabels[0]} 等 ${selectedLabels.length} 项`;
                }
                label.textContent = displayLabel;
                toggle.title = selectedLabels.join("、");
                toggle.classList.toggle("has-selection", selectedLabels.length > 0);
                toggle.setAttribute("aria-label", `${fieldLabel}：${displayLabel}`);
            };

            const filterOptions = () => {
                const query = normalizeFilterValue(search.value);
                let visibleCount = 0;
                optionRows.forEach((row) => {
                    const matches = !query
                        || normalizeFilterValue(
                            row.dataset.multiSelectSearchText || row.textContent,
                        ).includes(query);
                    row.hidden = !matches;
                    if (matches) {
                        visibleCount += 1;
                    }
                });
                if (emptyEl) {
                    emptyEl.hidden = visibleCount !== 0;
                }
            };

            const closePanel = () => {
                panel.hidden = true;
                toggle.setAttribute("aria-expanded", "false");
            };

            const openPanel = () => {
                document.querySelectorAll("[data-multi-select-panel]").forEach((otherPanel) => {
                    if (otherPanel === panel) {
                        return;
                    }
                    otherPanel.hidden = true;
                    const otherSelect = otherPanel.closest("[data-multi-select]");
                    const otherToggle = otherSelect
                        ? otherSelect.querySelector("[data-multi-select-toggle]")
                        : null;
                    if (otherToggle) {
                        otherToggle.setAttribute("aria-expanded", "false");
                    }
                });
                document.querySelectorAll("[data-date-picker-panel]").forEach((datePanel) => {
                    datePanel.hidden = true;
                    const datePicker = datePanel.closest("[data-date-picker]");
                    const dateToggle = datePicker
                        ? datePicker.querySelector("[data-date-picker-toggle]")
                        : null;
                    if (dateToggle) {
                        dateToggle.setAttribute("aria-expanded", "false");
                    }
                });
                search.value = "";
                filterOptions();
                panel.hidden = false;
                toggle.setAttribute("aria-expanded", "true");
            };

            toggle.addEventListener("click", () => {
                if (panel.hidden) {
                    openPanel();
                } else {
                    closePanel();
                }
            });
            panel.addEventListener("click", (event) => {
                event.stopPropagation();
            });
            search.addEventListener("input", filterOptions);
            filter.addEventListener("change", updateLabel);
            if (clearBtn) {
                clearBtn.addEventListener("click", () => {
                    checkboxes.forEach((input) => {
                        input.checked = false;
                    });
                    filter.dispatchEvent(new Event("change", {bubbles: true}));
                });
            }
            if (doneBtn) {
                doneBtn.addEventListener("click", closePanel);
            }
            document.addEventListener("click", (event) => {
                if (!multiSelect.contains(event.target)) {
                    closePanel();
                }
            });
            document.addEventListener("keydown", (event) => {
                if (event.key === "Escape" && !panel.hidden) {
                    closePanel();
                    toggle.focus({preventScroll: true});
                }
            });

            updateLabel();
            filterOptions();
        });
    }

    function setupNewsFilters() {
        document.querySelectorAll("[data-news-filter-scope]").forEach((scope) => {
            if (scope.dataset.bound === "1") {
                return;
            }
            scope.dataset.bound = "1";

            const filters = Array.from(scope.querySelectorAll("[data-news-filter]"));
            const cards = Array.from(scope.querySelectorAll("[data-news-card]"));
            const groups = Array.from(scope.querySelectorAll("[data-news-group]"));
            const countEl = scope.querySelector("[data-news-count]");
            const emptyEl = scope.querySelector("[data-news-empty]");
            const clearBtn = scope.querySelector("[data-news-clear]");
            const dedupeBtn = scope.querySelector("[data-news-dedupe]");
            const searchInput = scope.querySelector("[data-news-search]");
            const viewButtons = Array.from(scope.querySelectorAll("[data-news-view]"));
            const persistKey = scope.dataset.newsFilterPersistKey || "";
            const stateKey = persistKey ? `ai-news:filters:${persistKey}` : "";
            let dedupeEnabled = false;

            const readState = () => {
                if (!stateKey) {
                    return {};
                }
                try {
                    return JSON.parse(window.sessionStorage.getItem(stateKey) || "{}");
                } catch (_error) {
                    return {};
                }
            };

            const writeState = () => {
                if (!stateKey) {
                    return;
                }
                const state = {
                    filters: {},
                    search: searchInput ? searchInput.value : "",
                    view: scope.classList.contains("view-compact") ? "compact" : "comfortable",
                };
                filters.forEach((filter) => {
                    state.filters[filter.dataset.newsFilter] = readFilterValues(filter);
                });
                try {
                    window.sessionStorage.setItem(stateKey, JSON.stringify(state));
                } catch (_error) {
                    // Filtering remains functional without browser storage.
                }
            };

            const savedState = readState();
            filters.forEach((filter) => {
                const savedValues = savedState.filters && savedState.filters[filter.dataset.newsFilter];
                if (!Array.isArray(savedValues) || filter.dataset.newsFilter === "date") {
                    return;
                }
                const savedSet = new Set(savedValues.map(normalizeFilterValue));
                if (filter.dataset.newsFilterMode === "checkboxes") {
                    filter.querySelectorAll("input[type='checkbox']").forEach((input) => {
                        input.checked = savedSet.has(normalizeFilterValue(input.value));
                    });
                } else if (filter.matches("select[multiple]")) {
                    Array.from(filter.options).forEach((option) => {
                        option.selected = savedSet.has(normalizeFilterValue(option.value));
                    });
                } else if (filter.matches("select")) {
                    const matchingOption = Array.from(filter.options).find(
                        (option) => savedSet.has(normalizeFilterValue(option.value)),
                    );
                    filter.value = matchingOption ? matchingOption.value : "";
                }
            });
            if (searchInput && typeof savedState.search === "string") {
                searchInput.value = savedState.search;
            }
            if (savedState.view === "compact") {
                scope.classList.add("view-compact");
            }
            viewButtons.forEach((button) => {
                button.classList.toggle(
                    "active",
                    button.dataset.newsView === (savedState.view || "comfortable"),
                );
            });

            const applyFilters = (anchor = null) => {
                const apply = () => {
                const criteria = {};
                filters.forEach((filter) => {
                    criteria[filter.dataset.newsFilter] = readFilterValues(filter);
                });

                let visibleCount = 0;
                const searchValue = normalizeFilterValue(searchInput ? searchInput.value : "");
                const baseMatches = new Map();
                cards.forEach((card) => {
                    const matches = Object.entries(criteria).every(([key, expectedValues]) => {
                        if (!expectedValues.length) {
                            return true;
                        }
                        const cardValues = readCardFilterValues(card, key);
                        return expectedValues.some(
                            (expected) => cardValues.includes(expected),
                        );
                    });
                    const matchesSearch = !searchValue
                        || normalizeFilterValue(card.dataset.search).includes(searchValue);
                    baseMatches.set(card, matches && matchesSearch);
                });

                const dedupeKeep = new Map();
                if (dedupeEnabled) {
                    cards.forEach((card) => {
                        if (!baseMatches.get(card)) {
                            return;
                        }
                        const group = card.dataset.crossDateGroup;
                        if (!group) {
                            return;
                        }
                        const rank = Number.parseInt(card.dataset.crossDateRank || "0", 10);
                        const current = dedupeKeep.get(group);
                        if (!current || rank < current.rank) {
                            dedupeKeep.set(group, {card, rank});
                        }
                    });
                }

                cards.forEach((card) => {
                    const matches = Boolean(baseMatches.get(card));
                    const group = card.dataset.crossDateGroup;
                    const matchesDedupe = !dedupeEnabled
                        || !group
                        || dedupeKeep.get(group)?.card === card;
                    card.hidden = !(matches && matchesDedupe);
                    if (matches && matchesDedupe) {
                        visibleCount += 1;
                    }
                });

                groups.forEach((group) => {
                    const groupCards = Array.from(group.querySelectorAll("[data-news-card]"));
                    const groupVisibleCount = groupCards.filter((card) => !card.hidden).length;
                    const groupCountEl = group.querySelector("[data-news-group-count]");
                    group.hidden = groupVisibleCount === 0;
                    if (groupCountEl) {
                        groupCountEl.textContent = String(groupVisibleCount);
                    }
                });

                if (countEl) {
                    countEl.textContent = String(visibleCount);
                }
                if (emptyEl) {
                    emptyEl.hidden = visibleCount !== 0;
                }
                writeState();
                };
                if (anchor) {
                    keepControlInPlace(anchor, apply);
                } else {
                    apply();
                }
            };

            filters.forEach((filter) => {
                filter.addEventListener("change", () => applyFilters(filter));
            });
            if (clearBtn) {
                clearBtn.addEventListener("click", () => {
                    keepControlInPlace(clearBtn, () => {
                        filters.forEach((filter) => {
                            clearFilter(filter);
                        });
                        if (searchInput) {
                            searchInput.value = "";
                        }
                        applyFilters();
                    });
                });
            }
            if (dedupeBtn) {
                dedupeBtn.addEventListener("click", () => {
                    keepControlInPlace(dedupeBtn, () => {
                        dedupeEnabled = !dedupeEnabled;
                        dedupeBtn.classList.toggle("active", dedupeEnabled);
                        dedupeBtn.setAttribute(
                            "aria-pressed",
                            dedupeEnabled ? "true" : "false",
                        );
                        dedupeBtn.textContent = dedupeEnabled ? "取消去重" : "一键去重";
                        applyFilters();
                    });
                });
            }
            if (searchInput) {
                searchInput.addEventListener("input", () => applyFilters(searchInput));
            }
            viewButtons.forEach((button) => {
                button.addEventListener("click", () => {
                    keepControlInPlace(button, () => {
                        const compact = button.dataset.newsView === "compact";
                        scope.classList.toggle("view-compact", compact);
                        viewButtons.forEach((item) => {
                            item.classList.toggle("active", item === button);
                        });
                        writeState();
                    });
                });
            });
            applyFilters();
        });
    }

    function setupTableSearch() {
        document.querySelectorAll("[data-table-search]").forEach((input) => {
            const table = document.getElementById(input.dataset.tableSearch || "");
            if (!table) {
                return;
            }
            const rows = Array.from(table.querySelectorAll("[data-table-row]"));
            input.addEventListener("input", () => {
                keepControlInPlace(input, () => {
                    const query = normalizeFilterValue(input.value);
                    rows.forEach((row) => {
                        const hidden = Boolean(query)
                            && !normalizeFilterValue(row.textContent).includes(query);
                        row.hidden = hidden;
                        const editRow = row.nextElementSibling;
                        if (editRow && editRow.classList.contains("source-edit-row") && hidden) {
                            editRow.hidden = true;
                        }
                    });
                });
            });
        });
    }

    function setupTaskBuilder() {
        document.querySelectorAll("[data-check-all], [data-check-none]").forEach((button) => {
            const targetId = button.dataset.checkAll || button.dataset.checkNone;
            const target = document.getElementById(targetId);
            if (!target) {
                return;
            }
            button.addEventListener("click", () => {
                const checked = Boolean(button.dataset.checkAll);
                target.querySelectorAll("input[type='checkbox']").forEach((input) => {
                    input.checked = checked;
                });
                target.dispatchEvent(new Event("change", {bubbles: true}));
            });
        });

        document.querySelectorAll(".task-builder").forEach((builder) => {
            const categoryGroup = builder.querySelector("#source-categories");
            const hint = builder.querySelector("[data-selection-hint]");
            if (!categoryGroup || !hint) {
                return;
            }
            const updateHint = () => {
                const selected = categoryGroup.querySelectorAll("input:checked").length;
                hint.innerHTML = selected
                    ? `已选择 <strong>${selected}</strong> 个板块，仅抓取这些范围。`
                    : "当前为全量模式，将抓取全部启用信息源（含自定义信息源）。";
            };
            categoryGroup.addEventListener("change", updateHint);
            updateHint();
        });

        document.querySelectorAll("[data-submit-lock]").forEach((form) => {
            form.addEventListener("submit", () => {
                const button = form.querySelector("button[type='submit']");
                if (!button || button.disabled) {
                    return;
                }
                button.disabled = true;
                const label = button.querySelector("span");
                if (label) {
                    label.textContent = "任务创建中…";
                } else {
                    button.textContent = "任务创建中…";
                }
            });
        });

        document.querySelectorAll("[data-toggle-secret]").forEach((button) => {
            const input = document.getElementById(button.dataset.toggleSecret || "");
            if (!input) {
                return;
            }
            button.addEventListener("click", () => {
                const reveal = input.type === "password";
                input.type = reveal ? "text" : "password";
                button.textContent = reveal ? "隐藏" : "显示";
            });
        });
    }

    function setupScrollRestoration() {
        let scrollTimer = null;
        window.addEventListener("scroll", () => {
            if (scrollTimer !== null) {
                window.clearTimeout(scrollTimer);
            }
            scrollTimer = window.setTimeout(() => {
                scrollTimer = null;
                saveScrollPosition();
            }, 80);
        }, {passive: true});

        document.querySelectorAll("form").forEach((form) => {
            form.addEventListener("submit", () => {
                saveScrollPosition();
            });
        });

        const saved = savedScrollPosition();
        if (!saved || !Number.isFinite(saved.y)) {
            return;
        }
        const restore = () => {
            const maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
            window.scrollTo({
                top: Math.min(saved.y, maxY),
                left: Number.isFinite(saved.x) ? saved.x : 0,
                behavior: "auto",
            });
        };
        window.requestAnimationFrame(() => {
            restore();
            window.requestAnimationFrame(restore);
        });
        window.setTimeout(restore, 80);
        window.setTimeout(restore, 240);
        window.addEventListener("pageshow", restore, {once: true});
    }

    function setupSourceEditRows() {
        const savedEditor = storageRead(sourceEditorStorageKey, {});
        const savedTargetId = savedEditor[currentScrollPath()] || "";
        document.querySelectorAll("[data-source-edit-toggle]").forEach((toggle) => {
            if (toggle.dataset.bound === "1") {
                return;
            }
            toggle.dataset.bound = "1";
            const targetId = toggle.getAttribute("aria-controls");
            const target = targetId ? document.getElementById(targetId) : null;
            if (!target) {
                return;
            }
            if (target.id === savedTargetId) {
                target.hidden = false;
                toggle.setAttribute("aria-expanded", "true");
                toggle.textContent = "收起编辑";
            }
            toggle.addEventListener("click", () => {
                const isOpen = !target.hidden;
                keepControlInPlace(toggle, () => {
                    target.hidden = isOpen;
                    toggle.setAttribute("aria-expanded", String(!isOpen));
                    toggle.textContent = isOpen ? "编辑" : "收起编辑";
                });
                const editors = storageRead(sourceEditorStorageKey, {});
                if (isOpen) {
                    delete editors[currentScrollPath()];
                } else {
                    editors[currentScrollPath()] = target.id;
                }
                storageWrite(sourceEditorStorageKey, editors);
            });
        });
    }

    function boot() {
        showQueuedSystemNotices();
        setupLocalDateTimes();
        setupModelSelectors();
        setupRunEvents();
        setupDatePickers();
        setupNewsFilters();
        setupMultiSelectFilters();
        setupTableSearch();
        setupTaskBuilder();
        setupSourceEditRows();
        setupScrollRestoration();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
    window.addEventListener("pagehide", () => {
        saveScrollPosition();
        if (activeRunSource) {
            activeRunSource.close();
            activeRunSource = null;
            activeRunId = "";
        }
    });
}());
