# Merge Sort vs Quick Sort: Detailed Comparison

## Overview

Both merge sort and quick sort are divide-and-conquer algorithms commonly used for efficient sorting. While they share similar time complexity in the average case, they differ significantly in space requirements, stability, and practical performance.

---

## Time Complexity

| Metric | Merge Sort | Quick Sort |
|--------|-----------|-----------|
| **Best Case** | O(n log n) | O(n log n) |
| **Average Case** | O(n log n) | O(n log n) |
| **Worst Case** | O(n log n) | O(n²) |

**Key Difference:** Merge sort guarantees O(n log n) in all cases, while quick sort can degrade to O(n²) when the pivot selection is poor (e.g., always selecting the smallest/largest element).

---

## Space Complexity

| Metric | Merge Sort | Quick Sort |
|--------|-----------|-----------|
| **Auxiliary Space** | O(n) | O(log n) |
| **Total Space** | O(n) | O(n) total, but O(log n) extra |

**Key Difference:**
- **Merge sort** requires O(n) extra space for the temporary arrays used during merging
- **Quick sort** uses O(log n) extra space for the recursion stack (with good pivot selection), making it more space-efficient

---

## Stability

| Property | Merge Sort | Quick Sort |
|----------|-----------|-----------|
| **Stable?** | Yes | No |

**What does stability mean?** When sorting objects with equal keys, a stable sort preserves their original relative order.

**Example:**
```
Original: [(3, 'a'), (1, 'b'), (3, 'c'), (2, 'd')]
Sorting by first element:

Merge Sort output: [(1, 'b'), (2, 'd'), (3, 'a'), (3, 'c')]  ✓ stable
Quick Sort output: [(1, 'b'), (2, 'd'), (3, 'c'), (3, 'a')]  ✗ not stable
```

The pair (3, 'a') comes before (3, 'c') in merge sort (preserving original order), but may be reversed in quick sort.

---

## Code Examples: Python Implementation

### Merge Sort

```python
def merge_sort(arr):
    """
    Divide-and-conquer sorting algorithm.
    Time: O(n log n) | Space: O(n)
    """
    if len(arr) <= 1:
        return arr

    # Divide
    mid = len(arr) // 2
    left = arr[:mid]
    right = arr[mid:]

    # Conquer
    left_sorted = merge_sort(left)
    right_sorted = merge_sort(right)

    # Merge
    return merge(left_sorted, right_sorted)


def merge(left, right):
    """Merge two sorted arrays while maintaining stability."""
    result = []
    i = j = 0

    # Compare and merge
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:  # <= preserves stability
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1

    # Add remaining elements
    result.extend(left[i:])
    result.extend(right[j:])

    return result


# Example usage
arr = [38, 27, 43, 3, 9, 82, 10]
print("Merge Sort:", merge_sort(arr))
# Output: [3, 9, 10, 27, 38, 43, 82]
```

### Quick Sort

```python
def quick_sort(arr):
    """
    Divide-and-conquer sorting algorithm with in-place partitioning.
    Time: O(n log n) avg, O(n²) worst | Space: O(log n)
    """
    if len(arr) <= 1:
        return arr

    # Choose pivot (using middle element for better average performance)
    pivot_index = len(arr) // 2
    pivot = arr[pivot_index]

    # Partition
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    # Recursively sort and combine
    return quick_sort(left) + middle + quick_sort(right)


def quick_sort_inplace(arr, low=0, high=None):
    """
    In-place quick sort variant (more space-efficient).
    Time: O(n log n) avg, O(n²) worst | Space: O(log n)
    """
    if high is None:
        high = len(arr) - 1

    if low < high:
        # Partition and get pivot position
        pivot_index = partition(arr, low, high)

        # Recursively sort left and right
        quick_sort_inplace(arr, low, pivot_index - 1)
        quick_sort_inplace(arr, pivot_index + 1, high)

    return arr


def partition(arr, low, high):
    """Partition array using Lomuto partitioning scheme."""
    # Choose pivot (using high element; alternatives: random, median-of-three)
    pivot = arr[high]
    i = low - 1

    for j in range(low, high):
        if arr[j] < pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]

    # Place pivot in correct position
    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1


# Example usage
arr1 = [38, 27, 43, 3, 9, 82, 10]
print("Quick Sort (simple):", quick_sort(arr1))
# Output: [3, 9, 10, 27, 38, 43, 82]

arr2 = [38, 27, 43, 3, 9, 82, 10]
quick_sort_inplace(arr2)
print("Quick Sort (in-place):", arr2)
# Output: [3, 9, 10, 27, 38, 43, 82]
```

---

## Practical Performance Comparison

```python
import time
import random

def benchmark_sorts(size=10000):
    """Compare performance on real data."""
    data = [random.randint(0, 1000) for _ in range(size)]

    # Merge Sort
    arr1 = data.copy()
    start = time.time()
    merge_sort(arr1)
    merge_time = time.time() - start

    # Quick Sort (in-place)
    arr2 = data.copy()
    start = time.time()
    quick_sort_inplace(arr2)
    quick_time = time.time() - start

    print(f"Dataset size: {size}")
    print(f"Merge Sort: {merge_time:.4f}s")
    print(f"Quick Sort: {quick_time:.4f}s")
    print(f"Ratio (Quick/Merge): {quick_time/merge_time:.2f}x")

benchmark_sorts(10000)
# Typical output: Quick Sort is 1.5-2x faster in practice
```

---

## Stability Example

```python
# Demonstrate stability difference
class Person:
    def __init__(self, name, age):
        self.name = name
        self.age = age

    def __repr__(self):
        return f"{self.name}({self.age})"

    def __lt__(self, other):
        return self.age < other.age

people = [
    Person("Alice", 30),
    Person("Bob", 25),
    Person("Charlie", 30),
    Person("Diana", 25)
]

# Merge sort preserves original order for equal ages
def sort_by_age_merge(people):
    return merge_sort(people)

# Quick sort may not preserve order for equal ages
def sort_by_age_quick(people):
    return quick_sort(people)

print("Original:", people)
print("Merge Sort:", sort_by_age_merge(people[:]))
# Output: [Bob(25), Diana(25), Alice(30), Charlie(30)] ✓

print("Quick Sort:", sort_by_age_quick(people[:]))
# Output: [Diana(25), Bob(25), Charlie(30), Alice(30)] or varies ✗
```

---

## Typical Use Cases

### Use **Merge Sort** when:

1. **Stability is required** — Sorting database records by one field while preserving insertion order
2. **Guaranteed O(n log n) performance is critical** — Real-time systems where worst-case matters
3. **External sorting** — Sorting data too large for memory (sequential access patterns)
4. **Linked lists** — No random access penalty; O(n) space is acceptable
5. **Parallel sorting** — Easy to parallelize; independent subproblems

**Examples:**
- Sorting database results by timestamp (stability important)
- File merge operations in version control systems
- Sorting in databases (PostgreSQL uses merge sort for large datasets)

### Use **Quick Sort** when:

1. **Space is constrained** — Embedded systems, real-time memory limitations
2. **Average-case performance matters** — General-purpose sorting with typical random data
3. **In-place sorting needed** — Minimal extra memory requirements
4. **Cache locality important** — Arrays with good CPU cache behavior
5. **Simple implementation needed** — Less code, easier to understand

**Examples:**
- Standard library sorts (Python's `sorted()` uses Timsort, but Java's `Arrays.sort()` uses Quick Sort)
- Quickselect algorithm for finding k-th smallest element
- Sorting arrays in memory-constrained environments
- General-purpose sorting where stability isn't required

---

## Summary Table

| Criterion | Merge Sort | Quick Sort |
|-----------|-----------|-----------|
| **Best Case** | O(n log n) | O(n log n) |
| **Average Case** | O(n log n) | O(n log n) |
| **Worst Case** | O(n log n) | O(n²) |
| **Space** | O(n) | O(log n) |
| **Stable** | Yes | No |
| **In-place** | No | Yes |
| **Cache Friendly** | Moderate | Excellent |
| **Parallelizable** | Easy | Moderate |
| **Typical Speed** | Slower | Faster (2x avg) |

---

## Real-World Recommendation

- **Default choice for arrays**: Use **Quick Sort** (faster, better cache locality)
- **When stability matters**: Use **Merge Sort** or stable variant (like Tim Sort)
- **Modern Python/Java**: These languages use hybrid algorithms (Tim Sort, Dual Pivot Quick Sort) combining advantages of both
