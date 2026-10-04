/**
 * Panel Resize Module
 *
 * Generic resizable panel system. Configure via PanelResize.configure()
 * from Python with app-specific selectors and constraints.
 *
 * Supports multiple resizable panels with per-panel size memory.
 * Only one top panel and one bottom panel can be visible at a time,
 * but all panels remember their individual dimensions.
 */

(function() {
    'use strict';

    // ========== Configuration ==========
    // All app-specific values come from configure() call
    let config = {
        storageKey: 'panel_sizes',
        selectors: {
            topContainer: null,
            bottomContainer: null,
            controlPanel: null,
            bottomCovers: [],
            columnCovers: []
        },
        constraints: {
            viewportMarginX: 80,
            viewportMarginY: 100,
            totalMargin: 36
        },
        panels: {}
    };

    let configured = false;
    let appReady = false;

    // ========== Per-Panel Size Storage ==========
    let panelSizes = {};

    function loadPanelSizes() {
        try {
            const saved = localStorage.getItem(config.storageKey);
            if (saved) {
                panelSizes = JSON.parse(saved);
                console.log('[PanelResize] Loaded panel sizes:', panelSizes);
                // Set CSS variables immediately so they're available before panels exist
                for (const [panelId, sizes] of Object.entries(panelSizes)) {
                    if (sizes.height) {
                        document.documentElement.style.setProperty(`--panel-height-${panelId}`, sizes.height + 'px');
                    }
                    if (sizes.width) {
                        document.documentElement.style.setProperty(`--panel-width-${panelId}`, sizes.width + 'px');
                    }
                }
            }
        } catch (e) {
            console.warn('[PanelResize] Could not load panel sizes:', e);
            panelSizes = {};
        }
    }

    function savePanelSizes() {
        try {
            localStorage.setItem(config.storageKey, JSON.stringify(panelSizes));
        } catch (e) {
            console.warn('[PanelResize] Could not save panel sizes:', e);
        }
    }

    // ========== Active Tab Storage ==========
    const ACTIVE_TABS_KEY = 'parol_active_tabs';
    let activeTabs = { top: null, bottom: null, panel: null };

    function loadActiveTabs() {
        try {
            const saved = localStorage.getItem(ACTIVE_TABS_KEY);
            if (saved) {
                activeTabs = JSON.parse(saved);
                console.log('[PanelResize] Loaded active tabs:', activeTabs);
            }
        } catch (e) {
            console.warn('[PanelResize] Could not load active tabs:', e);
            activeTabs = { top: null, bottom: null, panel: null };
        }
    }

    function saveActiveTabs() {
        try {
            localStorage.setItem(ACTIVE_TABS_KEY, JSON.stringify(activeTabs));
        } catch (e) {
            console.warn('[PanelResize] Could not save active tabs:', e);
        }
    }

    function getActiveTabs() {
        return { ...activeTabs };
    }

    // Until the app is ready, tab changes are the page's own start-up
    // selection and restore; recording them would overwrite what is restored.
    function rememberTab(group, tab) {
        if (!appReady) return;
        activeTabs[group] = tab || null;
        saveActiveTabs();
    }

    // ========== Panel Identification ==========

    function getPanelId(panel) {
        if (panel.dataset && panel.dataset.panelId) {
            return panel.dataset.panelId;
        }
        for (const [panelId, panelConfig] of Object.entries(config.panels)) {
            if (panelConfig.selector && panel.matches(panelConfig.selector)) {
                return panelId;
            }
        }
        for (const className of panel.classList) {
            if (className.endsWith('-panel') && className !== 'resizable-panel') {
                return className.replace('-panel', '');
            }
        }
        return null;
    }

    function getPanelGroup(panel) {
        const panelId = getPanelId(panel);
        if (panelId && config.panels[panelId]) {
            return config.panels[panelId].group;
        }
        if (config.selectors.bottomContainer) {
            const bottomContainer = document.querySelector(config.selectors.bottomContainer);
            if (bottomContainer && bottomContainer.contains(panel)) {
                return 'bottom';
            }
        }
        if (config.selectors.topContainer) {
            const topContainer = document.querySelector(config.selectors.topContainer);
            if (topContainer && topContainer.contains(panel)) {
                return 'top';
            }
        }
        return 'unknown';
    }

    // ========== Visibility Helpers ==========

    /**
     * Find the currently visible resizable panel in a group.
     * Returns the panel element or null if none is visible.
     */
    function getVisibleResizablePanel(group) {
        const containerSelector = group === 'top'
            ? config.selectors.topContainer
            : config.selectors.bottomContainer;
        if (!containerSelector) return null;

        const container = document.querySelector(containerSelector);
        if (!container) return null;

        // Find a visible panel with .resizable-panel class
        // Check for display:none and also if parent tab-panel is active
        const panels = container.querySelectorAll('.resizable-panel');
        for (const panel of panels) {
            // Check if panel is visible (not display:none and parent is active)
            if (panel.offsetParent !== null) {
                return panel;
            }
            // Also check by parent q-tab-panel active state
            const tabPanel = panel.closest('.q-tab-panel');
            if (tabPanel && !tabPanel.classList.contains('q-tab-panel--inactive')) {
                return panel;
            }
        }
        return null;
    }

    /**
     * Check if height coupling should be active.
     * Returns true when both a resizable top and bottom panel are visible.
     */
    function shouldCouple() {
        const topResizable = getVisibleResizablePanel('top');
        const bottomResizable = getVisibleResizablePanel('bottom');
        if (topResizable && isFullHeightPanel(getPanelId(topResizable))) return false;
        return topResizable !== null && bottomResizable !== null;
    }

    /**
     * Get the first configured panel ID for a group.
     * Used for size persistence when no specific panel is visible.
     */
    function getFirstPanelIdForGroup(group) {
        for (const [panelId, panelConfig] of Object.entries(config.panels)) {
            if (panelConfig.group === group) {
                return panelId;
            }
        }
        return null;
    }

    // ========== Size Management ==========

    function savePanelSize(panel, width, height) {
        const panelId = getPanelId(panel);
        if (!panelId) return;

        if (!panelSizes[panelId]) {
            panelSizes[panelId] = {};
        }
        if (width !== undefined && width !== null) {
            panelSizes[panelId].width = width;
            document.documentElement.style.setProperty(`--panel-width-${panelId}`, width + 'px');
        }
        if (height !== undefined && height !== null) {
            panelSizes[panelId].height = height;
            document.documentElement.style.setProperty(`--panel-height-${panelId}`, height + 'px');
        }
        panelSizes[panelId].group = getPanelGroup(panel);

        console.log('[PanelResize] Saved size for', panelId + ':', panelSizes[panelId]);
        savePanelSizes();
    }

    function getSavedPanelSize(panelId) {
        return panelSizes[panelId] || null;
    }

    // ========== Fit to Content ==========
    // A panel configured with `fit` has no height of its own until the user
    // drags one: its container carries no inline height, so it is as tall as
    // its content (CSS caps it at the viewport). A dragged height is saved and
    // pins it from then on.

    function isFitPanel(panelId) {
        const cfg = panelId && config.panels[panelId];
        if (!cfg || !cfg.fit) return false;
        const saved = panelSizes[panelId];
        return !(saved && saved.height);
    }

    // A full-height panel is a column: CSS pins it between the top margin and
    // the footer, so its height is never dragged, saved or pushed.
    function isFullHeightPanel(panelId) {
        const cfg = panelId && config.panels[panelId];
        return !!(cfg && cfg.fullHeight);
    }

    // ========== Shell geometry ==========
    // What covers the 3D view: the column's right edge and the height the
    // footer and bottom panel take from the bottom. The scene frames itself
    // from the `wc:layout` event; CSS reads --wc-column-cover (the column
    // stops above it and above the bottom plugin panels) and
    // --wc-control-inset (the bottom panel stops short of the control panel).

    let columnOpen = false;
    let coupled = false;
    let layout = { columnRight: 0, bottomCover: 0 };
    let layoutFrame = 0;

    function setRootPx(name, value) {
        const style = document.documentElement.style;
        if (style.getPropertyValue(name) !== value + 'px') style.setProperty(name, value + 'px');
    }

    // An empty container still sits at its bottom offset; it covers nothing.
    function coverFromBottom(element) {
        if (!element || element.offsetParent === null) return 0;
        const rect = element.getBoundingClientRect();
        return rect.height > 0 ? Math.max(0, Math.round(window.innerHeight - rect.top)) : 0;
    }

    function coverOf(selectors) {
        let cover = 0;
        for (const selector of selectors) {
            cover = Math.max(cover, coverFromBottom(document.querySelector(selector)));
        }
        return cover;
    }

    function publishLayout() {
        layoutFrame = 0;
        let columnRight = 0;
        const container = columnOpen ? getContainer('top') : null;
        if (container) {
            columnRight = Math.round(container.getBoundingClientRect().left + container.offsetWidth);
        }
        const bottomCover = coverOf(config.selectors.bottomCovers);
        const columnCover = Math.max(bottomCover, coverOf(config.selectors.columnCovers));
        const controlPanel = config.selectors.controlPanel ? document.querySelector(config.selectors.controlPanel) : null;
        const inset = controlPanel && controlPanel.offsetParent !== null
            ? Math.max(0, Math.round(window.innerWidth - controlPanel.getBoundingClientRect().left))
            : 0;
        if (controlPanel) setRootPx('--wc-control-height', Math.ceil(controlPanel.getBoundingClientRect().height));
        setRootPx('--wc-control-inset', inset);
        setRootPx('--wc-column-cover', columnCover);
        layout = { columnRight: columnRight, bottomCover: bottomCover };
        window.dispatchEvent(new CustomEvent('wc:layout', { detail: { ...layout } }));
    }

    function scheduleLayout() {
        if (!layoutFrame) layoutFrame = requestAnimationFrame(publishLayout);
    }

    function getContainer(group) {
        const selector = group === 'top'
            ? config.selectors.topContainer
            : config.selectors.bottomContainer;
        return selector ? document.querySelector(selector) : null;
    }

    // Coupling may have pinned a fit panel to make room; let it fit again.
    function releaseFitHeight(group) {
        const panel = getVisibleResizablePanel(group);
        const container = getContainer(group);
        if (panel && container && isFitPanel(getPanelId(panel))) {
            container.style.removeProperty('height');
        }
    }

    // ========== Configuration Helpers ==========

    // Floor for a panel that declares no minimum; plugins may leave them unset.
    const DEFAULT_MIN = { width: 200, height: 100 };

    function getPanelConfig(panel) {
        const panelId = getPanelId(panel);

        if (panelId && config.panels[panelId]) {
            const cfg = config.panels[panelId];
            return {
                minWidth: cfg.minWidth ?? DEFAULT_MIN.width,
                minHeight: cfg.minHeight ?? DEFAULT_MIN.height,
                maxWidth: getMaxWidth(),
                maxHeight: getMaxHeight(),
                group: cfg.group,
                pushTarget: cfg.pushTarget
            };
        }

        return {
            minWidth: DEFAULT_MIN.width,
            minHeight: DEFAULT_MIN.height,
            maxWidth: getMaxWidth(),
            maxHeight: getMaxHeight(),
            group: 'unknown',
            pushTarget: null
        };
    }

    function getMaxWidth() {
        return window.innerWidth - config.constraints.viewportMarginX;
    }

    function getMaxHeight() {
        return window.innerHeight - config.constraints.viewportMarginY;
    }

    // A default width is a suggestion; a width the operator dragged is kept.
    function defaultWidth(container, panelConfig) {
        const width = panelConfig.defaultWidth || panelConfig.minWidth;
        return width ? Math.max(width, panelConfig.minWidth || 0) : width;
    }

    // ========== Resize State ==========
    let isResizing = false;
    let resizeType = null;
    let startX = 0;
    let startY = 0;
    let startWidth = 0;
    let startHeight = 0;
    let activePanel = null;
    let activeHandle = null;
    let invertHeight = false;
    let lastPushedPanel = null;

    // ========== Resize Logic ==========

    function onMouseMove(e) {
        if (!isResizing || !activePanel) return;

        // Only configured panels can be resized
        const panelId = getPanelId(activePanel);
        if (!panelId || !config.panels[panelId]) return;

        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const clientY = e.touches ? e.touches[0].clientY : e.clientY;
        const panelConfig = getPanelConfig(activePanel);

        // Handle width resize - only set container, panel fills via CSS
        if (resizeType === 'width' || resizeType === 'both') {
            const deltaX = clientX - startX;
            let newWidth = startWidth + deltaX;
            newWidth = Math.max(panelConfig.minWidth, Math.min(newWidth, panelConfig.maxWidth));

            const containerSelector = panelConfig.group === 'top'
                ? config.selectors.topContainer
                : config.selectors.bottomContainer;
            if (containerSelector) {
                const container = activePanel.closest(containerSelector);
                if (container) {
                    container.style.setProperty('width', newWidth + 'px', 'important');
                }
            }
            if (isFullHeightPanel(panelId)) scheduleLayout();
        }

        // Handle height resize - only set containers, panels fill via CSS
        if (resizeType === 'height' || resizeType === 'both') {
            let deltaY = clientY - startY;
            if (invertHeight) deltaY = -deltaY;

            let newHeight = startHeight + deltaY;

            const topContainer = config.selectors.topContainer ? document.querySelector(config.selectors.topContainer) : null;
            const bottomContainer = config.selectors.bottomContainer ? document.querySelector(config.selectors.bottomContainer) : null;

            const viewportHeight = window.innerHeight;
            const availableHeight = viewportHeight - config.constraints.totalMargin;

            // Use visibility helpers instead of class-based checks
            const topResizable = getVisibleResizablePanel('top');
            const bottomResizable = getVisibleResizablePanel('bottom');
            const isCoupled = topResizable && bottomResizable;

            // Get the other container for push logic
            const isTop = panelConfig.group === 'top';
            const otherPanel = isTop ? bottomResizable : topResizable;
            const otherContainer = isTop ? bottomContainer : topContainer;
            const otherPanelConfig = otherPanel ? getPanelConfig(otherPanel) : null;
            const otherMinHeight = otherPanelConfig ? otherPanelConfig.minHeight : DEFAULT_MIN.height;

            // Constrain to min/max
            newHeight = Math.max(panelConfig.minHeight, Math.min(newHeight, availableHeight));

            // Coupled mode: push the other container
            if (isCoupled && otherContainer) {
                const otherHeight = otherContainer.offsetHeight;

                if ((newHeight + otherHeight) > availableHeight) {
                    let newOtherHeight = availableHeight - newHeight;
                    if (newOtherHeight < otherMinHeight) {
                        // Can't push further - limit this container instead
                        newOtherHeight = otherMinHeight;
                        newHeight = availableHeight - otherMinHeight;
                    }
                    otherContainer.style.setProperty('height', newOtherHeight + 'px', 'important');
                    lastPushedPanel = otherPanel;
                }
            }

            // Apply height to active container
            const activeContainer = isTop ? topContainer : bottomContainer;
            if (activeContainer) {
                activeContainer.style.setProperty('height', newHeight + 'px', 'important');
            }
        }
    }

    function onMouseUp() {
        if (!isResizing) return;

        // Save only what was dragged: a width drag is not a choice of height.
        if (activePanel) {
            const dragsWidth = resizeType === 'width' || resizeType === 'both';
            const dragsHeight = resizeType === 'height' || resizeType === 'both';
            savePanelSize(
                activePanel,
                dragsWidth ? activePanel.offsetWidth : null,
                dragsHeight ? activePanel.offsetHeight : null
            );
        }

        // A pushed fixed panel keeps the height it was pushed to; a pushed fit
        // panel gives the room back once coupling ends.
        if (lastPushedPanel && !isFitPanel(getPanelId(lastPushedPanel))) {
            savePanelSize(lastPushedPanel, null, lastPushedPanel.offsetHeight);
        }

        isResizing = false;
        resizeType = null;
        invertHeight = false;
        lastPushedPanel = null;
        if (activeHandle) activeHandle.classList.remove('dragging');
        document.body.classList.remove('resizing-panel');
        document.body.style.cursor = '';
        activePanel = null;
        activeHandle = null;
        scheduleLayout();
    }

    // ========== Handle Attachment ==========

    function attachRightHandle(handle, panel) {
        if (handle._resizeAttached) return;
        handle._resizeAttached = true;

        function start(e) {
            e.preventDefault();
            e.stopPropagation();
            isResizing = true;
            resizeType = 'width';
            startX = e.touches ? e.touches[0].clientX : e.clientX;
            startWidth = panel.offsetWidth;
            activePanel = panel;
            activeHandle = handle;
            lastPushedPanel = null;
            handle.classList.add('dragging');
            document.body.classList.add('resizing-panel');
            document.body.style.cursor = 'ew-resize';
        }

        handle.addEventListener('mousedown', start);
        handle.addEventListener('touchstart', start, { passive: false });
    }

    function attachBottomHandle(handle, panel) {
        if (handle._resizeAttached) return;
        handle._resizeAttached = true;

        function start(e) {
            e.preventDefault();
            e.stopPropagation();
            isResizing = true;
            resizeType = 'height';
            invertHeight = false;
            startY = e.touches ? e.touches[0].clientY : e.clientY;
            startHeight = panel.offsetHeight;
            activePanel = panel;
            activeHandle = handle;
            lastPushedPanel = null;
            handle.classList.add('dragging');
            document.body.classList.add('resizing-panel');
            document.body.style.cursor = 'ns-resize';
        }

        handle.addEventListener('mousedown', start);
        handle.addEventListener('touchstart', start, { passive: false });
    }

    function attachTopHandle(handle, panel) {
        if (handle._resizeAttached) return;
        handle._resizeAttached = true;

        function start(e) {
            e.preventDefault();
            e.stopPropagation();
            isResizing = true;
            resizeType = 'height';
            invertHeight = true;
            startY = e.touches ? e.touches[0].clientY : e.clientY;
            lastPushedPanel = null;

            // Get container height for bottom panels, panel height otherwise
            const panelConfig = getPanelConfig(panel);
            if (panelConfig.group === 'bottom' && config.selectors.bottomContainer) {
                const bottomContainer = document.querySelector(config.selectors.bottomContainer);
                startHeight = bottomContainer ? bottomContainer.offsetHeight : panel.offsetHeight;
            } else {
                startHeight = panel.offsetHeight;
            }

            activePanel = panel;
            activeHandle = handle;
            handle.classList.add('dragging');
            document.body.classList.add('resizing-panel');
            document.body.style.cursor = 'ns-resize';
        }

        handle.addEventListener('mousedown', start);
        handle.addEventListener('touchstart', start, { passive: false });
    }

    function attachCornerHandle(handle, panel, isTopCorner) {
        if (handle._resizeAttached) return;
        handle._resizeAttached = true;

        function start(e) {
            e.preventDefault();
            e.stopPropagation();
            isResizing = true;
            resizeType = 'both';
            invertHeight = isTopCorner;
            startX = e.touches ? e.touches[0].clientX : e.clientX;
            startY = e.touches ? e.touches[0].clientY : e.clientY;
            startWidth = panel.offsetWidth;
            lastPushedPanel = null;

            // Get container height for bottom panels, panel height otherwise
            const panelConfig = getPanelConfig(panel);
            if (isTopCorner && panelConfig.group === 'bottom' && config.selectors.bottomContainer) {
                const bottomContainer = document.querySelector(config.selectors.bottomContainer);
                startHeight = bottomContainer ? bottomContainer.offsetHeight : panel.offsetHeight;
            } else {
                startHeight = panel.offsetHeight;
            }

            activePanel = panel;
            activeHandle = handle;
            handle.classList.add('dragging');
            document.body.classList.add('resizing-panel');
            document.body.style.cursor = isTopCorner ? 'nesw-resize' : 'nwse-resize';
        }

        handle.addEventListener('mousedown', start);
        handle.addEventListener('touchstart', start, { passive: false });
    }

    // ========== Panel Initialization ==========

    function initPanel(panel) {
        if (panel._panelResizeInit) return;
        panel._panelResizeInit = true;

        const rightHandle = panel.querySelector('.resize-handle-right');
        const bottomHandle = panel.querySelector('.resize-handle-bottom');
        const topHandle = panel.querySelector('.resize-handle-top');
        const cornerHandle = panel.querySelector('.resize-handle-corner');

        if (rightHandle) attachRightHandle(rightHandle, panel);
        if (bottomHandle) attachBottomHandle(bottomHandle, panel);
        if (topHandle) attachTopHandle(topHandle, panel);

        if (cornerHandle) {
            const isTopCorner = getPanelGroup(panel) === 'bottom';
            attachCornerHandle(cornerHandle, panel, isTopCorner);
        }

        console.log('[PanelResize] Panel initialized:', getPanelId(panel));
    }

    function initAllPanels() {
        const selectors = [];
        for (const panelConfig of Object.values(config.panels)) {
            if (panelConfig.selector) {
                selectors.push(panelConfig.selector);
            }
        }
        selectors.push('.resizable-panel');

        const resizablePanels = document.querySelectorAll(selectors.join(', '));

        const panelSet = new Set();
        resizablePanels.forEach(panel => {
            if (config.selectors.topContainer && panel.matches(config.selectors.topContainer)) return;
            if (config.selectors.bottomContainer && panel.matches(config.selectors.bottomContainer)) return;
            panelSet.add(panel);
        });

        panelSet.forEach(initPanel);
        console.log('[PanelResize] Initialized', panelSet.size, 'panels');
    }

    // ========== Tab Change Handling ==========

    function onTabChange(group, toTab) {
        console.log('[PanelResize] Tab change:', group, '->', toTab);

        rememberTab(group, toTab);

        const containerSelector = group === 'top'
            ? config.selectors.topContainer
            : config.selectors.bottomContainer;
        const container = containerSelector ? document.querySelector(containerSelector) : null;

        const isClosing = !toTab;

        // Check if the new tab has a resizable panel (use config, not DOM query)
        const isResizableTab = !isClosing && config.panels[toTab] !== undefined;

        if (isClosing) {
            // Save current size before closing. A fit panel's height is its
            // content's, and a column's is the viewport's: neither is a choice
            // to remember.
            const panel = getVisibleResizablePanel(group);
            const panelId = panel && getPanelId(panel);
            if (panel && container && !isFitPanel(panelId) && !isFullHeightPanel(panelId)) {
                const currentHeight = container.offsetHeight;
                if (currentHeight > 0) {
                    savePanelSize(panel, null, currentHeight);
                }
            }

            // Clear container constraints
            if (container) {
                container.style.removeProperty('height');
                container.style.removeProperty('width');
            }

            coupled = false;
            if (group === 'top') columnOpen = false;
        } else if (!isResizableTab) {
            // Non-resizable tab - clear container constraints
            if (container) {
                container.style.removeProperty('width');
                container.style.removeProperty('height');
            }

            coupled = false;
            if (group === 'top') columnOpen = false;
        } else {
            // Resizable tab - set container size BEFORE panel animates in
            // Panel ID matches tab name (e.g., "program", "gripper")
            const panelId = toTab;
            const savedSize = getSavedPanelSize(panelId) || {};
            const panelConfig = config.panels[panelId] || {};
            const column = isFullHeightPanel(panelId);

            if (group === 'top') columnOpen = column;
            if (container) {
                const width = savedSize.width || defaultWidth(container, panelConfig);
                if (width) {
                    container.style.width = Math.min(width, getMaxWidth()) + 'px';
                }
                if (column || isFitPanel(panelId)) {
                    container.style.removeProperty('height');
                } else {
                    const height = savedSize.height || panelConfig.defaultHeight || panelConfig.minHeight
                        || Math.min(Math.floor(window.innerHeight * 0.5), 500);
                    container.style.height = Math.min(height, getMaxHeight()) + 'px';
                }
                console.log('[PanelResize] Pre-set container size:', container.style.width, 'x', container.style.height || 'content');
            }
        }

        scheduleLayout();
        // Update coupling state after Quasar finishes animating the panel
        setTimeout(updateCouplingState, 350);
    }

    /**
     * Update coupling state based on current panel visibility.
     * Called after tab changes to sync CSS class with actual state.
     * When coupling is activated, ensures panels don't overlap.
     */
    function updateCouplingState() {
        const wasCoupled = coupled;
        coupled = shouldCouple();

        if (coupled) {
            // When coupling is first activated, ensure panels don't overlap
            if (!wasCoupled) {
                ensureNoOverlap();
            }
        } else {
            releaseFitHeight('top');
            releaseFitHeight('bottom');
        }
    }

    /**
     * Ensure top and bottom panels don't overlap.
     * Called when coupling is first activated.
     * Strategy: ensure both panels meet minimums first, then 50/50 if both are above minimums.
     */
    function ensureNoOverlap() {
        const topContainer = config.selectors.topContainer ? document.querySelector(config.selectors.topContainer) : null;
        const bottomContainer = config.selectors.bottomContainer ? document.querySelector(config.selectors.bottomContainer) : null;

        if (!topContainer || !bottomContainer) return;

        const viewportHeight = window.innerHeight;
        const availableHeight = viewportHeight - config.constraints.totalMargin;
        const gap = 12;

        const topHeight = topContainer.offsetHeight;
        const bottomHeight = bottomContainer.offsetHeight;
        const totalHeight = topHeight + bottomHeight + gap;

        if (totalHeight <= availableHeight) return; // No overlap

        const topPanel = getVisibleResizablePanel('top');
        const bottomPanel = getVisibleResizablePanel('bottom');
        const topMinHeight = topPanel ? getPanelConfig(topPanel).minHeight : 300;
        const bottomMinHeight = bottomPanel ? getPanelConfig(bottomPanel).minHeight : DEFAULT_MIN.height;
        const usableHeight = availableHeight - gap;

        let newTopHeight = topHeight;
        let newBottomHeight = bottomHeight;
        const topFits = topPanel && isFitPanel(getPanelId(topPanel));
        const bottomFits = bottomPanel && isFitPanel(getPanelId(bottomPanel));

        if (topFits || bottomFits) {
            // A panel sized by its content gives way before one the user sized,
            // and of two such panels the taller gives way. Neither is raised to
            // its minimum: content shorter than that is already small enough.
            const topGives = topFits && bottomFits ? topHeight >= bottomHeight : topFits;
            if (topGives) {
                const floor = Math.min(topMinHeight, topHeight);
                newTopHeight = Math.max(floor, usableHeight - bottomHeight);
                newBottomHeight = Math.min(bottomHeight, usableHeight - newTopHeight);
            } else {
                const floor = Math.min(bottomMinHeight, bottomHeight);
                newBottomHeight = Math.max(floor, usableHeight - topHeight);
                newTopHeight = Math.min(topHeight, usableHeight - newBottomHeight);
            }
        } else {
            // First: ensure both panels meet their minimums
            if (topHeight < topMinHeight) {
                newTopHeight = topMinHeight;
                newBottomHeight = usableHeight - newTopHeight;
            }
            if (bottomHeight < bottomMinHeight) {
                newBottomHeight = bottomMinHeight;
                newTopHeight = usableHeight - newBottomHeight;
            }

            // If both are above minimums but still overlapping, split 50/50
            if (newTopHeight + newBottomHeight > usableHeight) {
                newTopHeight = Math.floor(usableHeight / 2);
                newBottomHeight = usableHeight - newTopHeight;
            }
        }

        // Apply heights to containers only - panels fill via CSS. A container
        // left as it was keeps fitting its content.
        if (newTopHeight !== topHeight) {
            topContainer.style.setProperty('height', newTopHeight + 'px', 'important');
        }
        if (newBottomHeight !== bottomHeight) {
            bottomContainer.style.setProperty('height', newBottomHeight + 'px', 'important');
        }

        console.log('[PanelResize] Adjusted heights to prevent overlap:', { newTopHeight, newBottomHeight });
    }

    // ========== App Ready Signal ==========

    function onAppReady() {
        console.log('[PanelResize] App ready signal received');
        appReady = true;
        initAllPanels();
        observeContainers();
    }

    // A fit panel changes height with its content (a form opening, a section
    // expanding) after coupling was settled, so re-check for overlap then.
    let containerObserver = null;
    function observeContainers() {
        if (containerObserver || !('ResizeObserver' in window)) return;
        containerObserver = new ResizeObserver(function() {
            scheduleLayout();
            if (isResizing || !appReady) return;
            if (shouldCouple()) ensureNoOverlap();
        });
        ['top', 'bottom'].forEach(function(group) {
            const container = getContainer(group);
            if (container) containerObserver.observe(container);
        });
        const layoutObserver = new ResizeObserver(scheduleLayout);
        const watched = [config.selectors.controlPanel, ...config.selectors.bottomCovers];
        for (const selector of watched) {
            const element = selector ? document.querySelector(selector) : null;
            if (!element) continue;
            layoutObserver.observe(element);
            // A transform scales what is published without resizing the
            // box, so a resize can publish a size mid-transition and nothing
            // would republish the size it settles at. Transitions inside it
            // (a blink, a hover) bubble here too and change no layout, and
            // every publish redraws the scene.
            const settled = function(e) {
                if (e.target === element) scheduleLayout();
            };
            element.addEventListener('transitionend', settled);
            element.addEventListener('transitioncancel', settled);
        }
    }

    // ========== Viewport Resize Handler ==========

    function onViewportResize() {
        const maxW = getMaxWidth();
        const maxH = getMaxHeight();

        for (const group of ['top', 'bottom']) {
            const container = getContainer(group);
            if (!container) continue;
            const panel = getVisibleResizablePanel(group);
            const panelId = panel && getPanelId(panel);
            const saved = panelId ? getSavedPanelSize(panelId) : null;
            if (panelId && config.panels[panelId] && !(saved && saved.width)) {
                // A width nobody dragged follows the window, as it did on opening.
                const width = defaultWidth(container, config.panels[panelId]);
                container.style.setProperty('width', Math.min(width, maxW) + 'px');
            } else if (container.offsetWidth > maxW) {
                container.style.setProperty('width', maxW + 'px', 'important');
            }
            // CSS caps a fit panel and pins a column; pinning either here would
            // stick after the window grows back.
            const fits = panel && (isFitPanel(panelId) || isFullHeightPanel(panelId));
            if (!fits && container.offsetHeight > maxH) {
                container.style.setProperty('height', maxH + 'px', 'important');
            }
        }
        updateCouplingState();
        scheduleLayout();
    }

    // ========== Global Event Listeners ==========

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    document.addEventListener('touchmove', onMouseMove, { passive: false });
    document.addEventListener('touchend', onMouseUp);

    let resizeTimeout = null;
    window.addEventListener('resize', function() {
        document.body.classList.add('viewport-resizing');
        if (resizeTimeout) clearTimeout(resizeTimeout);
        requestAnimationFrame(onViewportResize);
        resizeTimeout = setTimeout(function() {
            document.body.classList.remove('viewport-resizing');
        }, 150);
    });

    // ========== Configuration API ==========

    function configure(userConfig) {
        if (!userConfig) return;

        if (userConfig.storageKey) {
            config.storageKey = userConfig.storageKey;
        }
        if (userConfig.selectors) {
            Object.assign(config.selectors, userConfig.selectors);
        }
        if (userConfig.constraints) {
            Object.assign(config.constraints, userConfig.constraints);
        }
        if (userConfig.panels) {
            for (const [panelId, panelConfig] of Object.entries(userConfig.panels)) {
                config.panels[panelId] = {
                    ...config.panels[panelId],
                    ...panelConfig,
                    selector: panelConfig.selector || `.${panelId}-panel`
                };
            }
        }

        configured = true;
        console.log('[PanelResize] Configured:', config);

        loadPanelSizes();
        forgetDefaultHeightsOfFitPanels();
        forgetOldDefaultSizes();
        loadActiveTabs();
    }

    // The editor had no default size and opened at its minimum, which closing
    // then saved as if chosen. A size equal to that minimum was not a choice.
    const OLD_DEFAULT_SIZES = { program: { width: 450, height: 300 } };

    function forgetOldDefaultSizes() {
        const doneKey = config.storageKey + '_defaults';
        try {
            if (localStorage.getItem(doneKey)) return;
        } catch (e) {
            return;
        }
        let changed = false;
        for (const [panelId, old] of Object.entries(OLD_DEFAULT_SIZES)) {
            const saved = panelSizes[panelId];
            if (!saved) continue;
            for (const dim of ['width', 'height']) {
                if (saved[dim] === old[dim]) {
                    delete saved[dim];
                    document.documentElement.style.removeProperty(`--panel-${dim}-${panelId}`);
                    changed = true;
                }
            }
        }
        if (changed) savePanelSizes();
        try {
            localStorage.setItem(doneKey, '1');
        } catch (e) {
            console.warn('[PanelResize] Could not record the default-size migration:', e);
        }
    }

    // Older builds saved a height for every panel on close, so the heights
    // stored for panels that now fit their content were defaults, not
    // choices. Drop them once.
    function forgetDefaultHeightsOfFitPanels() {
        const doneKey = config.storageKey + '_fit';
        try {
            if (localStorage.getItem(doneKey)) return;
        } catch (e) {
            return;
        }
        let changed = false;
        for (const [panelId, cfg] of Object.entries(config.panels)) {
            if (cfg.fit && panelSizes[panelId] && panelSizes[panelId].height) {
                delete panelSizes[panelId].height;
                document.documentElement.style.removeProperty(`--panel-height-${panelId}`);
                changed = true;
            }
        }
        if (changed) savePanelSizes();
        try {
            localStorage.setItem(doneKey, '1');
        } catch (e) {
            console.warn('[PanelResize] Could not record the fit migration:', e);
        }
    }

    // ========== Setup ==========

    function init() {
        loadPanelSizes();
        loadActiveTabs();

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function() {
                setTimeout(initAllPanels, 200);
            });
        } else {
            setTimeout(initAllPanels, 200);
        }

        function setupObserver() {
            if (!document.body) {
                setTimeout(setupObserver, 50);
                return;
            }

            const observer = new MutationObserver(function(mutations) {
                let shouldInit = false;
                mutations.forEach(function(mutation) {
                    if (mutation.addedNodes.length > 0) {
                        mutation.addedNodes.forEach(function(node) {
                            if (node.nodeType === 1) {
                                if (node.classList && node.classList.contains('resizable-panel')) {
                                    shouldInit = true;
                                } else if (node.querySelector && node.querySelector('.resizable-panel')) {
                                    shouldInit = true;
                                }
                            }
                        });
                    }
                });
                if (shouldInit) {
                    setTimeout(initAllPanels, 100);
                }
            });

            observer.observe(document.body, { childList: true, subtree: true });
            console.log('[PanelResize] Observer attached');
        }

        setupObserver();
    }

    init();

    // ========== Public API ==========

    /**
     * Programmatically resize a panel to specific dimensions or a named preset.
     * @param {string} panelId - Panel identifier (e.g. "gripper")
     * @param {string|object} dims - "default", "camera", or {width, height}
     */
    function resizePanel(panelId, dims) {
        const panelCfg = config.panels[panelId];
        if (!panelCfg) return;

        let width, height;
        if (typeof dims === 'string') {
            if (dims === 'camera') {
                width = panelCfg.cameraWidth;
                height = panelCfg.cameraHeight;
            } else {
                width = panelCfg.defaultWidth;
                height = panelCfg.defaultHeight;
            }
        } else {
            width = dims.width;
            height = dims.height;
        }

        // Clamp to constraints
        const maxW = getMaxWidth();
        const maxH = getMaxHeight();
        if (width) width = Math.max(panelCfg.minWidth ?? DEFAULT_MIN.width, Math.min(width, maxW));
        if (height) height = Math.max(panelCfg.minHeight ?? DEFAULT_MIN.height, Math.min(height, maxH));
        if (isFullHeightPanel(panelId)) height = null;

        // The container is shared by its group's panels: size it only for
        // this one. A hidden panel gets its size when its tab opens.
        const panel = document.querySelector(panelCfg.selector);
        const container = getContainer(panelCfg.group);
        if (panel && panel.offsetParent !== null && container) {
            if (width) container.style.setProperty('width', width + 'px', 'important');
            if (height) container.style.setProperty('height', height + 'px', 'important');
            scheduleLayout();
        }

        // Save to localStorage
        if (panel) {
            savePanelSize(panel, width, height);
        } else {
            // Panel not in DOM yet — save directly
            if (!panelSizes[panelId]) panelSizes[panelId] = {};
            if (width) {
                panelSizes[panelId].width = width;
                document.documentElement.style.setProperty(`--panel-width-${panelId}`, width + 'px');
            }
            if (height) {
                panelSizes[panelId].height = height;
                document.documentElement.style.setProperty(`--panel-height-${panelId}`, height + 'px');
            }
            panelSizes[panelId].group = panelCfg.group;
            savePanelSizes();
        }
    }

    window.PanelResize = {
        configure: configure,
        init: initAllPanels,
        initPanel: function(selector) {
            const panel = document.querySelector(selector);
            if (panel) initPanel(panel);
        },
        onTabChange: onTabChange,
        onAppReady: onAppReady,
        getSavedSize: getSavedPanelSize,
        getActiveTabs: getActiveTabs,
        rememberTab: rememberTab,
        resizePanel: resizePanel,
        clearAllSizes: function() {
            panelSizes = {};
            savePanelSizes();
            console.log('[PanelResize] Sizes cleared');
        },
        getConfig: function() { return config; },
        getSizes: function() { return panelSizes; },
        layout: function() { return { ...layout }; },
        isConfigured: function() { return configured; },
        isAppReady: function() { return appReady; }
    };

})();
