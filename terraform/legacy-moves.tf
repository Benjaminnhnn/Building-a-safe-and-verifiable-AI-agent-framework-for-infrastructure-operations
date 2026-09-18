# Preserve existing state addresses when enabling optional legacy resources.
moved {
  from = aws_instance.web
  to   = aws_instance.web[0]
}
moved {
  from = aws_instance.core
  to   = aws_instance.core[0]
}
moved {
  from = aws_eip.web
  to   = aws_eip.web[0]
}
moved {
  from = aws_eip.core
  to   = aws_eip.core[0]
}
