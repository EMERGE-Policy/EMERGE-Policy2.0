"""The robot folder: the files the three sides of the robot layer share, and the rules for them.

Nodes (`client`), the simulation (`simulation`) and front ends (`frontend`) never call each other; they
only read and write the files of a conversation's robot folder. What those files are, what they hold and
how they are written is decided here, once, and every side uses it. Nothing here knows about any side.

    paths        where each file lives; each body has a folder of its own
    fileio       how a file is written whole, and changed under a lock
    manifest     which body owns which arm and camera, written by hand and checked against the driver
    frames       poses in a body's frame and in the world's, one pair of functions for reading and sending
    lifecycle    what an action and a command have in common: their status, and how they end
    actions      action.json: who holds each arm of a body, and the actions sent to it
    commands     command.json: the one command a front end gave the simulation
    snapshots    state.json and observation.json: what one tick shows the nodes of one body

Two kinds of file, two rules:

    changed by both sides   action.json, command.json   read-modify-write under a lock (fileio.edit_json)
    published once          snapshots under obs/<tick>/  written whole, in an order that readers rely on
"""
