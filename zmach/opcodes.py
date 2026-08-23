"""Opcode and error-number constants (ZSpec 1.0 §14).

Opcode keys as produced by VM._decode:
  2OP form: 0..31          1OP form: 128..159
  0OP form: 176..191       VAR form: 224..255 (0xE0+ keys are the full byte)
  EXT form: 256 + ext-byte (prefix 0xBE, v5+)
"""

# Extended-opcode prefix (first byte of the extended form, v5+)
EXT_PREFIX = 0xBE
EXT_BASE = 256

# Max routine call depth (ZSpec §4.6: deeper is a stack-overflow error)
MAX_CALL_DEPTH = 63

# Error numbers (Inform conventions; the plan pins 8 = division by zero,
# 14 = stack overflow, 581 = uncaught throw)
ERR_ILLEGAL_OPCODE = 1
ERR_VAR_RANGE = 2
ERR_OBJ_RANGE = 3
ERR_ATTR_RANGE = 4
ERR_BAD_OPERAND = 5
ERR_NO_PROP = 6
ERR_BAD_PROP_NUM = 7
ERR_DIV_ZERO = 8
ERR_STACK_OVERFLOW = 14
ERR_UNCAUGHT_THROW = 581