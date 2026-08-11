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
            wrap: null,
            topContainer: null,
            bottomContainer: null
        },
        constraints: {
            viewportMarginX: 80,
            viewportMarginY: 100,
            containerPadding: 20,
            bottomOffset: 12,
            totalMargin: 36
        },
        stateClasses: {
            coupled: 'coupled'
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
    let activeTabs = { top: null, bottom: null };

    function loadActiveTabs() {
        try {
            const saved = localStorage.getItem(ACTIVE_TABS_KEY);
            if (saved) {
                activeTabs = JSON.parse(saved);
                console.log('[PanelResize] Loaded active tabs:', activeTabs);
            }
        } catch (e) {
            console.warn('[PanelResize] Could not load active tabs:', e);
            activeTabs = { top: null, bottom: null };
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

    // ========== Configuration Helpers ==========

    function getPanelConfig(panel) {
        const panelId = getPanelId(panel);

        if (panelId && config.panels[panelId]) {
            const cfg = config.panels[panelId];
            return {
                minWidth: cfg.minWidth,
                minHeight: cfg.minHeight,
                maxWidth: getMaxWidth(),
                maxHeight: getMaxHeight(),
                group: cfg.group,
                pushTarget: cfg.pushTarget
            };
        }

        return {
            minWidth: 200,
            minHeight: 100,
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
            const otherMinHeight = otherPanelConfig ? otherPanelConfig.minHeight : 100;

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

        // Save active panel size
        if (activePanel) {
            savePanelSize(activePanel, activePanel.offsetWidth, activePanel.offsetHeight);
        }

        // Save pushed panel size (the panel that was pushed during resize)
        if (lastPushedPanel) {
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

        // Only update and save active tab state after app is fully initialized
        // This prevents the initial auto-selection from overwriting saved state in memory
        if (appReady) {
            activeTabs[group] = toTab || null;
            saveActiveTabs();
        }

        const wrap = config.selectors.wrap ? document.querySelector(config.selectors.wrap) : null;
        const containerSelector = group === 'top'
            ? config.selectors.topContainer
            : config.selectors.bottomContainer;
        const container = containerSelector ? document.querySelector(containerSelector) : null;

        const isClosing = !toTab;

        // Check if the new tab has a resizable panel (use config, not DOM query)
        const isResizableTab = !isClosing && config.panels[toTab] !== undefined;

        if (isClosing) {
            // Save current size before closing
            const panel = getVisibleResizablePanel(group);
            if (panel && container) {
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

            if (wrap) {
                wrap.classList.remove(config.stateClasses.coupled);
            }
        } else if (!isResizableTab) {
            // Non-resizable tab - clear container constraints
            if (container) {
                container.style.removeProperty('width');
                container.style.removeProperty('height');
            }

            if (wrap) {
                wrap.classList.remove(config.stateClasses.coupled);
            }
        } else {
            // Resizable tab - set container size BEFORE panel animates in
            // Panel ID matches tab name (e.g., "program", "response")
            const panelId = toTab;
            const savedSize = getSavedPanelSize(panelId);
            const panelConfig = config.panels[panelId] || {};

            if (savedSize && container) {
                if (savedSize.width) {
                    container.style.width = savedSize.width + 'px';
                }
                if (savedSize.height) {
                    container.style.height = savedSize.height + 'px';
                }
                console.log('[PanelResize] Pre-set container size:', savedSize.width, 'x', savedSize.height);
            } else if (container) {
                // No saved size - use panel's declared defaults or minima
                const viewportHeight = window.innerHeight;
                const defaultHeight = panelConfig.defaultHeight || panelConfig.minHeight || Math.min(Math.floor(viewportHeight * 0.5), 500);
                container.style.height = defaultHeight + 'px';
                const defaultWidth = panelConfig.defaultWidth || panelConfig.minWidth;
                if (defaultWidth) {
                    container.style.width = Math.min(defaultWidth, getMaxWidth()) + 'px';
                }
                console.log('[PanelResize] Pre-set default container size:', defaultWidth, 'x', defaultHeight);
            }
        }

        // Update coupling state after Quasar finishes animating the panel
        setTimeout(updateCouplingState, 350);
    }

    /**
     * Update coupling state based on current panel visibility.
     * Called after tab changes to sync CSS class with actual state.
     * When coupling is activated, ensures panels don't overlap.
     */
    function updateCouplingState() {
        const wrap = config.selectors.wrap ? document.querySelector(config.selectors.wrap) : null;
        if (!wrap) return;

        const isCoupled = shouldCouple();
        const wasCoupled = wrap.classList.contains(config.stateClasses.coupled);

        if (isCoupled) {
            wrap.classList.add(config.stateClasses.coupled);

            // When coupling is first activated, ensure panels don't overlap
            if (!wasCoupled) {
                ensureNoOverlap();
            }
        } else {
            wrap.classList.remove(config.stateClasses.coupled);
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
        const bottomMinHeight = bottomPanel ? getPanelConfig(bottomPanel).minHeight : 100;
        const usableHeight = availableHeight - gap;

        let newTopHeight = topHeight;
        let newBottomHeight = bottomHeight;

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

        // Apply heights to containers only - panels fill via CSS
        topContainer.style.setProperty('height', newTopHeight + 'px', 'important');
        bottomContainer.style.setProperty('height', newBottomHeight + 'px', 'important');

        console.log('[PanelResize] Adjusted heights to prevent overlap:', { newTopHeight, newBottomHeight });
    }

    // ========== App Ready Signal ==========

    function onAppReady() {
        console.log('[PanelResize] App ready signal received');
        appReady = true;
        initAllPanels();
    }

    // ========== Viewport Resize Handler ==========

    function onViewportResize() {
        const maxW = getMaxWidth();

        for (const [panelId, panelConfig] of Object.entries(config.panels)) {
            if (!panelConfig.selector) continue;

            const containerSelector = panelConfig.group === 'top'
                ? config.selectors.topContainer
                : config.selectors.bottomContainer;
            if (!containerSelector) continue;

            const container = document.querySelector(containerSelector);
            if (!container) continue;

            const currentWidth = container.offsetWidth;
            if (currentWidth > maxW) {
                container.style.setProperty('width', maxW + 'px', 'important');
            }
        }
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
        if (userConfig.stateClasses) {
            Object.assign(config.stateClasses, userConfig.stateClasses);
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
        loadActiveTabs();
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
        if (width) width = Math.max(panelCfg.minWidth || 200, Math.min(width, maxW));
        if (height) height = Math.max(panelCfg.minHeight || 100, Math.min(height, maxH));

        // Find container
        const containerSelector = panelCfg.group === 'top'
            ? config.selectors.topContainer
            : config.selectors.bottomContainer;
        const container = containerSelector ? document.querySelector(containerSelector) : null;

        if (container) {
            if (width) container.style.setProperty('width', width + 'px', 'important');
            if (height) container.style.setProperty('height', height + 'px', 'important');
        }

        // Save to localStorage
        const panel = document.querySelector(panelCfg.selector);
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
        resizePanel: resizePanel,
        clearAllSizes: function() {
            panelSizes = {};
            savePanelSizes();
            console.log('[PanelResize] Sizes cleared');
        },
        getConfig: function() { return config; },
        getSizes: function() { return panelSizes; },
        isConfigured: function() { return configured; },
        isAppReady: function() { return appReady; }
    };

})();
