import SwiftUI

struct RootView: View {
    @Environment(\.scenePhase) private var scenePhase
    @State private var selectedTab: Tab = .chat
    @State private var showingSettings = false
    @State private var unauthorizedAlert = false
    @State private var settings: AppSettings
    @State private var chatStore: ChatStore
    @State private var agendaStore: AgendaStore
    @State private var logStore: LogStore

    init() {
        let settings = AppSettings()
        _settings = State(initialValue: settings)
        _chatStore = State(initialValue: ChatStore(settings: settings))
        _agendaStore = State(initialValue: AgendaStore(settings: settings))
        _logStore = State(initialValue: LogStore(settings: settings))
    }

    // Re-tapping the selected Chat tab triggers the system scroll-to-top;
    // for a bottom-anchored chat the right target is the newest message,
    // so intercept the re-selection and counter-scroll to the bottom.
    private var tabSelection: Binding<Tab> {
        Binding {
            selectedTab
        } set: { tab in
            if tab == selectedTab, tab == .chat {
                chatStore.requestScrollToBottom()
            }
            selectedTab = tab
        }
    }

    var body: some View {
        TabView(selection: tabSelection) {
            NavigationStack {
                AgendaView(store: agendaStore) { item in
                    chatStore.setPrefill(item.chatPrefill)
                    selectedTab = .chat
                } markDone: { item in
                    Task {
                        if await agendaStore.markDone(item) {
                            await chatStore.syncHistory()
                        }
                    }
                }
                .navigationTitle("Today")
                .toolbar { settingsToolbar }
            }
            .tabItem { Label("Today", systemImage: "calendar") }
            .tag(Tab.today)

            NavigationStack {
                ChatView(store: chatStore, settings: settings)
                    .navigationTitle("Inkling")
                    .toolbar {
                        ToolbarItem(placement: .topBarTrailing) {
                            Button {
                                Task { await chatStore.checkHealthAndSync() }
                            } label: {
                                Image(systemName: "arrow.clockwise")
                            }
                            .accessibilityLabel("Check connection and refresh")
                        }
                        settingsToolbar
                    }
            }
            .tabItem { Label("Chat", systemImage: "message") }
            .tag(Tab.chat)

            NavigationStack {
                LogView(store: logStore) { entry in
                    chatStore.setPrefill(entry.chatPrefill)
                    selectedTab = .chat
                }
                .navigationTitle("Log")
                .toolbar { settingsToolbar }
            }
            .tabItem { Label("Log", systemImage: "chart.bar") }
            .tag(Tab.log)
        }
        .sheet(isPresented: $showingSettings) {
            SettingsSheet(settings: settings)
        }
        .alert("Unauthorized — check your token in Settings.", isPresented: $unauthorizedAlert) {
            Button("OK", role: .cancel) { }
        }
        .onAppear {
            if !settings.hasToken { showingSettings = true }
        }
        .task {
            await refreshAll()
            await PushRegistrar.shared.ensureRegistered(settings: settings)
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            Task { await refreshAll() }
            Task { await LiveActivityRegistrar.shared.report() }
        }
        .onChange(of: showingSettings) { wasShowing, isShowing in
            guard wasShowing, !isShowing, settings.hasToken else { return }
            Task {
                await refreshAll()
                await PushRegistrar.shared.ensureRegistered(settings: settings)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .inklingUnauthorized)) { _ in
            unauthorizedAlert = true
        }
        .onReceive(NotificationCenter.default.publisher(for: .inklingDataShouldRefresh)) { _ in
            Task {
                async let agenda: Void = agendaStore.refresh()
                async let log: Void = logStore.refresh()
                _ = await (agenda, log)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .inklingOpenURL)) { _ in
            selectedTab = .chat
            Task { await chatStore.syncHistory() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .inklingCachesCleared)) { _ in
            chatStore.clearLocalCache()
            agendaStore.clearLocalCache()
            logStore.clearLocalCache()
        }
    }

    @ToolbarContentBuilder
    private var settingsToolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Button { showingSettings = true } label: {
                Image(systemName: "gearshape")
            }
            .accessibilityLabel("Settings")
        }
    }

    private func refreshAll() async {
        async let history: Void = chatStore.syncHistory()
        async let agenda: Void = agendaStore.refresh()
        async let log: Void = logStore.refresh()
        _ = await (history, agenda, log)
    }
}
