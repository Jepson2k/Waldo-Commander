"""PLC-style I/O cycle: wait for a start input, run the moves, signal at-home.

The classic cell handshake (WAIT DI / set DO on a teach pendant) written as
a program, so no GUI configuration is needed:
  1. Park at the home/standby pose and raise Output 2 ("robot at home").
  2. Block until Input 1 goes high (wire it to a PLC output or a button).
  3. Drop Output 2 and run one cycle of moves.
  4. Loop until stopped from the GUI (or e-stop).
"""

from parol6 import RobotClient

rbt = RobotClient(host="127.0.0.1", port=5001)

HOME = [90.0, -90.0, 180.0, 0.0, 0.0, 180.0]
AT_HOME_OUTPUT = 1  # "OUTPUT 2" on the I/O panel
START_INPUT = 0  # io = [in1, in2, out1, out2, estop]

rbt.home()

while True:
    rbt.move_j(HOME, speed=0.5, wait=True)
    rbt.write_io(AT_HOME_OUTPUT, 1)

    while not rbt.wait_status(lambda s: s.io[START_INPUT] == 1, timeout=60.0):
        pass

    rbt.write_io(AT_HOME_OUTPUT, 0)

    # One cycle of work — replace with your own moves.
    rbt.move_j([60.0, -75.0, 160.0, 0.0, -20.0, 180.0], speed=0.5, wait=True)
    rbt.move_j([120.0, -75.0, 160.0, 0.0, -20.0, 180.0], speed=0.5, wait=True)
