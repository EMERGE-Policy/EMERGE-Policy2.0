"""The full-screen interface of EMERGE-Policy 2.0, over `emerge` (the agent) and `robot` (the hardware).

    cli          the `emerge` command: this TUI on a terminal, emerge's headless mode otherwise
    app          the workspace screen: conversation, sidebar, commands; owns the conversation's simulation
    onboarding   before a conversation opens: the workspace's model settings, the robot and its scene
    state        what the screen shows, reduced from events
"""
