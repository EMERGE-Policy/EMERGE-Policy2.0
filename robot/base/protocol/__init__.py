"""The robot folder: the files the three sides of the robot layer share, and the rules for them.

Nodes (`client`), the simulation (`simulation`) and front ends (`frontend`) never call each other; they
only read and write the files of a conversation's robot folder. What those files are, what they hold and
how they are written is decided here, once, and every side uses it. Nothing here knows about any side.

    paths        where each file lives
    fileio       how a file is written whole, and changed under a lock
    lifecycle    what an action and a command have in common: their status, and how they end
    actions      ACTION.json: who holds each arm, and the actions sent to it
    commands     COMMAND.json: the one command a front end gave the simulation
    snapshots    STATE.json and OBSERVATION.json: what one tick shows the nodes

Two kinds of file, two rules:

    changed by both sides   ACTION.json, COMMAND.json   read-modify-write under a lock (fileio.edit_json)
    published once          snapshots under obs/<tick>/  written whole, in an order that readers rely on
"""
