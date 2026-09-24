"""The control plane: a thin web view over the task files of many repos.

It runs from the repositories alone. Each one is cloned into the app's own
data directory (:mod:`task_viewer.mirror`), refreshed lazily when a page is
loaded, and written back through commit-and-push. Nothing about an agent is
visible here except what it wrote into a task — which is the point.
"""
